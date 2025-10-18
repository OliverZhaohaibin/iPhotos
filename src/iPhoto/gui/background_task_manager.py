"""Utility that centralises background task submission for the GUI facade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal


@dataclass
class _TaskRecord:
    """Internal bookkeeping structure for a tracked background task."""

    worker: QRunnable
    pause_watcher: bool
    signals: Optional[QObject]
    connections: List[tuple[object, object]]


class BackgroundTaskManager(QObject):
    """Coordinate QRunnable execution and watcher state on behalf of the facade."""

    # ``taskStarted`` and ``taskFinished`` broadcast arbitrary payloads whose
    # shape depends on the worker implementation.  Declaring the parameters as
    # ``QVariant`` preserves that flexibility while giving Nuitka a concrete
    # meta-type instead of the Python ``object`` descriptor.
    taskStarted = Signal(str, "QVariant")
    taskProgress = Signal(str, int, int)
    taskError = Signal(str, str)
    taskFinished = Signal(str, "QVariant")

    def __init__(
        self,
        *,
        pause_watcher: Optional[Callable[[], None]] = None,
        resume_watcher: Optional[Callable[[], None]] = None,
        resume_delay_ms: int = 500,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._thread_pool = QThreadPool.globalInstance()
        self._pause_callback = pause_watcher
        self._resume_callback = resume_watcher
        self._resume_delay_ms = max(0, int(resume_delay_ms))
        self._active: Dict[str, _TaskRecord] = {}
        self._paused_tasks = 0

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------
    def is_busy(self) -> bool:
        """Return ``True`` while any tracked task is executing."""

        return bool(self._active)

    def has_watcher_blocking_tasks(self) -> bool:
        """Return ``True`` when at least one task paused the library watcher."""

        return self._paused_tasks > 0

    # ------------------------------------------------------------------
    # Task submission
    # ------------------------------------------------------------------
    def submit_task(
        self,
        task_id: str,
        worker: QRunnable,
        *,
        started: Optional[Signal] = None,
        progress: Optional[Signal] = None,
        finished: Signal,
        error: Optional[Signal] = None,
        pause_watcher: bool = False,
        on_finished: Callable[..., None],
        on_error: Optional[Callable[[str], None]] = None,
        result_payload: Optional[Callable[..., object]] = None,
    ) -> None:
        """Submit *worker* to the pool and propagate lifecycle events."""

        if task_id in self._active:
            raise ValueError(f"Task '{task_id}' is already active")

        connections: List[tuple[object, object]] = []
        record = _TaskRecord(worker=worker, pause_watcher=pause_watcher, signals=None, connections=connections)

        # ``started``, ``progress``, ``finished``, and ``error`` are expected to
        # originate from the worker-specific signal container, which in turn is
        # usually a ``QObject``.  Preserve a reference to keep the object alive
        # until the task completes.
        worker_signals = getattr(worker, "signals", None)
        if isinstance(worker_signals, QObject):
            record.signals = worker_signals

        self._active[task_id] = record

        if pause_watcher and self._pause_callback is not None:
            self._pause_callback()
            self._paused_tasks += 1

        if started is not None:
            handler = self._wrap_started(task_id)
            started.connect(handler)
            connections.append((started, handler))

        if progress is not None:
            handler = self._wrap_progress(task_id)
            progress.connect(handler)
            connections.append((progress, handler))

        if error is not None:
            handler = self._wrap_error(task_id, on_error)
            error.connect(handler)
            connections.append((error, handler))

        finish_handler = self._wrap_finished(task_id, on_finished, result_payload)
        finished.connect(finish_handler)
        connections.append((finished, finish_handler))

        self._thread_pool.start(worker)

    # ------------------------------------------------------------------
    # Signal wrappers
    # ------------------------------------------------------------------
    def _wrap_started(self, task_id: str):
        def _handler(*args) -> None:
            payload = args if len(args) != 1 else args[0]
            self.taskStarted.emit(task_id, payload)

        return _handler

    def _wrap_progress(self, task_id: str):
        def _handler(*args) -> None:
            if len(args) >= 3 and isinstance(args[1], int) and isinstance(args[2], int):
                self.taskProgress.emit(task_id, int(args[1]), int(args[2]))
            elif len(args) >= 2 and isinstance(args[0], int) and isinstance(args[1], int):
                self.taskProgress.emit(task_id, int(args[0]), int(args[1]))

        return _handler

    def _wrap_error(
        self,
        task_id: str,
        callback: Optional[Callable[..., None]],
    ):
        def _handler(*args) -> None:
            if not args:
                message = ""
            elif len(args) == 1:
                message = str(args[0])
            else:
                message = str(args[-1])
            self.taskError.emit(task_id, message)
            if callback is not None:
                callback(*args)

        return _handler

    def _wrap_finished(
        self,
        task_id: str,
        callback: Callable[..., None],
        payload_builder: Optional[Callable[..., object]],
    ):
        def _handler(*args) -> None:
            try:
                callback(*args)
                payload = payload_builder(*args) if payload_builder is not None else (args if len(args) != 1 else args[0])
                self.taskFinished.emit(task_id, payload)
            finally:
                self._cleanup(task_id)

        return _handler

    # ------------------------------------------------------------------
    # Cleanup helpers
    # ------------------------------------------------------------------
    def _cleanup(self, task_id: str) -> None:
        record = self._active.pop(task_id, None)
        if record is None:
            return

        for signal, handler in record.connections:
            try:
                signal.disconnect(handler)
            except (TypeError, RuntimeError):
                continue

        if record.signals is not None:
            record.signals.deleteLater()

        if record.pause_watcher and self._pause_callback is not None:
            self._paused_tasks = max(0, self._paused_tasks - 1)
            if self._paused_tasks == 0 and self._resume_callback is not None:
                QTimer.singleShot(self._resume_delay_ms, self._resume_callback)
