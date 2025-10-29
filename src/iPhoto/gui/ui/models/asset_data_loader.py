"""Async helpers for loading asset metadata into the asset list model."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QThreadPool, Signal

from ..tasks.asset_loader_worker import AssetLoaderSignals, AssetLoaderWorker, compute_asset_rows


class AssetDataLoader(QObject):
    """Wrap :class:`AssetLoaderWorker` to provide a minimal Qt friendly API."""

    chunkReady = Signal(Path, list)
    loadFinished = Signal(Path, bool)
    loadProgress = Signal(Path, int, int)
    error = Signal(Path, str)

    def __init__(self, parent: QObject | None = None) -> None:
        """Initialise the loader wrapper."""
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._worker: Optional[AssetLoaderWorker] = None
        self._signals: Optional[AssetLoaderSignals] = None

    def is_running(self) -> bool:
        """Return ``True`` while a worker is active."""
        return self._worker is not None

    def current_root(self) -> Optional[Path]:
        """Return the album root handled by the active worker, if any."""
        return self._worker.root if self._worker else None

    def start(
        self,
        root: Path,
        featured: List[Dict[str, object]],
        live_map: Dict[str, Dict[str, object]],
    ) -> None:
        """Launch a background worker for *root*."""
        if self._worker is not None:
            raise RuntimeError("Loader already running")
        signals = AssetLoaderSignals()
        signals.chunkReady.connect(self._handle_chunk_ready)
        signals.finished.connect(self._handle_finished)
        signals.progressUpdated.connect(self._handle_progress)
        signals.error.connect(self._handle_error)
        worker = AssetLoaderWorker(root, featured, signals, live_map)
        self._worker = worker
        self._signals = signals
        self._pool.start(worker)

    def cancel(self) -> None:
        """Request cancellation for the active worker."""
        if self._worker is None:
            return
        self._worker.cancel()

    def compute_rows(
        self,
        root: Path,
        featured: List[Dict[str, object]],
        live_map: Dict[str, Dict[str, object]],
    ) -> Tuple[List[Dict[str, object]], int]:
        """Synchronously compute asset rows for *root*.

        This is primarily used when the index file is small enough to load on the
        GUI thread without noticeably blocking the interface.  The logic mirrors
        what :class:`AssetLoaderWorker` performs in the background.
        """

        return compute_asset_rows(root, featured, live_map)

    def _handle_chunk_ready(self, root: Path, chunk: List[Dict[str, object]]) -> None:
        """Relay chunk notifications from the worker."""
        self.chunkReady.emit(root, chunk)

    def _handle_progress(self, root: Path, current: int, total: int) -> None:
        """Relay progress updates from the worker."""
        self.loadProgress.emit(root, current, total)

    def _handle_finished(self, root: Path, success: bool) -> None:
        """Relay completion notifications and tear down the worker."""
        self.loadFinished.emit(root, success)
        self._teardown()

    def _handle_error(self, root: Path, message: str) -> None:
        """Relay worker errors and tear down the worker."""
        self.error.emit(root, message)
        self._teardown()

    def _teardown(self) -> None:
        """Release references to worker objects."""
        if self._worker is not None:
            self._worker.signals.deleteLater()
        elif self._signals is not None:
            self._signals.deleteLater()
        self._worker = None
        self._signals = None
