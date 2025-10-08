"""Qt-aware facade that bridges the CLI backend to the GUI layer."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from .. import app as backend
from ..errors import IPhotoError
from ..models.album import Album

if TYPE_CHECKING:
    from .ui.tasks.scanner_worker import ScannerWorker


class AppFacade(QObject):
    """Expose high-level album operations to the GUI layer."""

    albumOpened = Signal(object)
    indexUpdated = Signal(object)
    linksUpdated = Signal(object)
    errorRaised = Signal(str)
    scanProgress = Signal(int, int)
    scanFinished = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._current_album: Optional[Album] = None
        self._scanner_thread: Optional[QThread] = None
        self._scanner_worker: Optional["ScannerWorker"] = None

    # ------------------------------------------------------------------
    # Album lifecycle
    # ------------------------------------------------------------------
    @property
    def current_album(self) -> Optional[Album]:
        """Return the album currently loaded in the facade."""

        return self._current_album

    def open_album(self, root: Path) -> Optional[Album]:
        """Open *root* and emit signals for the loaded data."""

        try:
            album = backend.open_album(root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return None
        self._current_album = album
        self.albumOpened.emit(album.root)
        self.indexUpdated.emit(album.root)
        self.linksUpdated.emit(album.root)
        return album

    def rescan_current(self) -> List[dict]:
        """Rescan the active album and emit ``indexUpdated`` when done."""

        album = self._require_album()
        if album is None:
            return []
        try:
            rows = backend.rescan(album.root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return []
        self.indexUpdated.emit(album.root)
        self.linksUpdated.emit(album.root)
        return rows

    def rescan_current_async(self) -> None:
        """Start a background rescan for the active album."""

        from .ui.tasks.scanner_worker import ScannerWorker

        album = self._require_album()
        if album is None:
            self.scanFinished.emit(False)
            return

        if self._scanner_thread and self._scanner_thread.isRunning():
            if self._scanner_worker is not None:
                self._scanner_worker.cancel()
            self._scanner_thread.quit()
            self._scanner_thread.wait()

        self._scanner_thread = QThread()
        include = album.manifest.get("filters", {}).get("include", backend.DEFAULT_INCLUDE)
        exclude = album.manifest.get("filters", {}).get("exclude", backend.DEFAULT_EXCLUDE)

        self._scanner_worker = ScannerWorker(album.root, include, exclude)
        self._scanner_worker.moveToThread(self._scanner_thread)

        self._scanner_thread.started.connect(self._scanner_worker.run)
        self._scanner_worker.progressUpdated.connect(self.scanProgress.emit)
        self._scanner_worker.finished.connect(self._on_scan_finished)
        self._scanner_worker.error.connect(self._on_scan_error)
        self._scanner_worker.finished.connect(self._scanner_thread.quit)
        self._scanner_worker.error.connect(self._scanner_thread.quit)
        self._scanner_thread.finished.connect(self._cleanup_scan_thread)

        self._scanner_thread.start()

    def pair_live_current(self) -> List[dict]:
        """Rebuild Live Photo pairings for the active album."""

        album = self._require_album()
        if album is None:
            return []
        try:
            groups = backend.pair(album.root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return []
        self.linksUpdated.emit(album.root)
        return [group.__dict__ for group in groups]

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------
    def set_cover(self, rel: str) -> bool:
        """Set the album cover to *rel* and persist the manifest."""

        album = self._require_album()
        if album is None:
            return False
        album.set_cover(rel)
        return self._save_manifest(album)

    def toggle_featured(self, ref: str) -> bool:
        """Toggle *ref* in the album's featured list."""

        album = self._require_album()
        if album is None:
            return False
        featured = album.manifest.setdefault("featured", [])
        if ref in featured:
            album.remove_featured(ref)
            changed = False
        else:
            album.add_featured(ref)
            changed = True
        if self._save_manifest(album):
            return changed
        return False

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------
    def _save_manifest(self, album: Album) -> bool:
        try:
            album.save()
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return False
        # Reload to ensure any concurrent edits are picked up.
        self._current_album = Album.open(album.root)
        self.albumOpened.emit(album.root)
        return True

    def _require_album(self) -> Optional[Album]:
        if self._current_album is None:
            self.errorRaised.emit("No album is currently open.")
            return None
        return self._current_album

    def _on_scan_finished(self, rows: List[dict]) -> None:
        album = self._require_album()
        if album is None:
            self.scanFinished.emit(False)
            return
        try:
            backend.IndexStore(album.root).write_rows(rows)
            backend._ensure_links(album.root, rows)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
        finally:
            self.indexUpdated.emit(album.root)
            self.linksUpdated.emit(album.root)
            self.scanFinished.emit(True)

    def _on_scan_error(self, message: str) -> None:
        self.errorRaised.emit(message)
        self.scanFinished.emit(False)

    def _cleanup_scan_thread(self) -> None:
        if self._scanner_worker is not None:
            self._scanner_worker.deleteLater()
            self._scanner_worker = None
        if self._scanner_thread is not None:
            self._scanner_thread.deleteLater()
            self._scanner_thread = None
