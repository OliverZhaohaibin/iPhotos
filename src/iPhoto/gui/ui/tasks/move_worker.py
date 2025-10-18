"""Worker that moves assets between albums on a background thread."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable, List

from PySide6.QtCore import QObject, QRunnable, Signal

from .... import app as backend
from ....errors import IPhotoError


class MoveSignals(QObject):
    """Qt signal bundle used by :class:`MoveWorker` to report progress."""

    started = Signal(Path, Path)
    progress = Signal(Path, int, int)
    # NOTE: Qt's meta-object system cannot parse typing information such as ``list[Path]``
    # when compiling the signal signature. Using the bare ``list`` type keeps the
    # signature compatible across PySide6 versions while still conveying that a Python
    # list containing :class:`pathlib.Path` objects will be emitted.
    finished = Signal(Path, Path, list, bool, bool)
    error = Signal(str)


class MoveWorker(QRunnable):
    """Move media files to a different album and trigger rescans."""

    def __init__(
        self,
        sources: Iterable[Path],
        source_root: Path,
        destination_root: Path,
        signals: MoveSignals,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._sources = [Path(path) for path in sources]
        self._source_root = Path(source_root)
        self._destination_root = Path(destination_root)
        self._signals = signals
        self._cancel_requested = False

    @property
    def signals(self) -> MoveSignals:
        """Expose the signal container to callers."""

        return self._signals

    def cancel(self) -> None:
        """Request cancellation of the move operation."""

        self._cancel_requested = True

    @property
    def cancelled(self) -> bool:
        """Return ``True`` when the worker was asked to stop early."""

        return self._cancel_requested

    def run(self) -> None:  # pragma: no cover - executed on a worker thread
        """Move the queued files while updating progress and rescanning albums."""

        total = len(self._sources)
        self._signals.started.emit(self._source_root, self._destination_root)
        if total == 0:
            self._signals.finished.emit(
                self._source_root,
                self._destination_root,
                [],
                True,
                True,
            )
            return

        moved: List[Path] = []
        for index, source in enumerate(self._sources, start=1):
            if self._cancel_requested:
                break
            try:
                target = self._move_into_destination(source)
            except FileNotFoundError:
                self._signals.error.emit(f"File not found: {source}")
            except OSError as exc:
                self._signals.error.emit(f"Could not move '{source}': {exc}")
            else:
                moved.append(target)
            finally:
                self._signals.progress.emit(self._source_root, index, total)

        rescan_source_ok = True
        rescan_destination_ok = True
        if moved and not self._cancel_requested:
            try:
                backend.rescan(self._source_root)
            except IPhotoError as exc:
                rescan_source_ok = False
                self._signals.error.emit(str(exc))
            try:
                backend.rescan(self._destination_root)
            except IPhotoError as exc:
                rescan_destination_ok = False
                self._signals.error.emit(str(exc))

        self._signals.finished.emit(
            self._source_root,
            self._destination_root,
            moved,
            rescan_source_ok,
            rescan_destination_ok,
        )

    def _move_into_destination(self, source: Path) -> Path:
        """Move *source* into the destination album avoiding name collisions."""

        if not source.exists():
            raise FileNotFoundError(source)
        target_dir = self._destination_root
        base_name = source.name
        target = target_dir / base_name
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = target_dir / f"{stem} ({counter}){suffix}"
            counter += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        moved_path = shutil.move(str(source), str(target))
        return Path(moved_path).resolve()


__all__ = ["MoveSignals", "MoveWorker"]
