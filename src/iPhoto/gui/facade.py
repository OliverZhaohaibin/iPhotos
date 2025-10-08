"""Qt-aware facade that bridges the CLI backend to the GUI layer."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from .. import app as backend
from ..config import WORK_DIR_NAME
from ..errors import IPhotoError
from ..models.album import Album

if TYPE_CHECKING:
    from .ui.tasks.scanner_worker import ScannerWorker
    from .ui.models.asset_list_model import AssetListModel


class AppFacade(QObject):
    """Expose high-level album operations to the GUI layer."""

    albumOpened = Signal(object)
    indexUpdated = Signal(object)
    linksUpdated = Signal(object)
    errorRaised = Signal(str)
    scanProgress = Signal(object, int, int)
    scanFinished = Signal(object, bool)
    loadStarted = Signal(object)
    loadProgress = Signal(object, int, int)
    loadFinished = Signal(object, bool)

    def __init__(self) -> None:
        super().__init__()
        self._current_album: Optional[Album] = None
        self._scanner_thread: Optional[QThread] = None
        self._scanner_worker: Optional["ScannerWorker"] = None

        from .ui.models.asset_list_model import AssetListModel

        self._asset_list_model = AssetListModel(self)
        self._asset_list_model.loadProgress.connect(self._on_model_load_progress)
        self._asset_list_model.loadFinished.connect(self._on_model_load_finished)

    # ------------------------------------------------------------------
    # Album lifecycle
    # ------------------------------------------------------------------
    @property
    def current_album(self) -> Optional[Album]:
        """Return the album currently loaded in the facade."""

        return self._current_album

    @property
    def asset_list_model(self) -> "AssetListModel":
        """Return the list model that backs the asset views."""

        return self._asset_list_model

    def open_album(self, root: Path) -> Optional[Album]:
        """Open *root* and trigger background work as needed."""

        try:
            album = Album.open(root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return None

        self._current_album = album
        album_root = album.root
        self.albumOpened.emit(album_root)

        index_path = album_root / WORK_DIR_NAME / "index.jsonl"
        has_index = False
        if index_path.exists():
            try:
                has_index = index_path.stat().st_size > 0
            except OSError:
                has_index = False

        if not has_index:
            self.rescan_current_async()
            return album

        self._restart_asset_load(album_root)
        self.indexUpdated.emit(album_root)
        self.linksUpdated.emit(album_root)
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
        self._restart_asset_load(album.root)
        return rows

    def rescan_current_async(self) -> None:
        """Start a background rescan for the active album."""

        from .ui.tasks.scanner_worker import ScannerWorker

        album = self._require_album()
        if album is None:
            self.scanFinished.emit(None, False)
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
        self._restart_asset_load(album.root)
        return True

    def _require_album(self) -> Optional[Album]:
        if self._current_album is None:
            self.errorRaised.emit("No album is currently open.")
            return None
        return self._current_album

    def _on_scan_finished(self, root: Path, rows: List[dict]) -> None:
        try:
            backend.IndexStore(root).write_rows(rows)
            backend._ensure_links(root, rows)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
        finally:
            self.indexUpdated.emit(root)
            self.linksUpdated.emit(root)
            self.scanFinished.emit(root, True)
            if self._current_album and self._current_album.root == root:
                self._restart_asset_load(root)

    def _on_scan_error(self, root: Path, message: str) -> None:
        self.errorRaised.emit(message)
        self.scanFinished.emit(root, False)

    def _cleanup_scan_thread(self) -> None:
        if self._scanner_worker is not None:
            self._scanner_worker.deleteLater()
            self._scanner_worker = None
        if self._scanner_thread is not None:
            self._scanner_thread.deleteLater()
            self._scanner_thread = None

    def _restart_asset_load(self, root: Path) -> None:
        if not (self._current_album and self._current_album.root == root):
            return
        self.loadStarted.emit(root)
        self._asset_list_model.start_load()

    def _on_model_load_progress(self, root: Path, current: int, total: int) -> None:
        self.loadProgress.emit(root, current, total)

    def _on_model_load_finished(self, root: Path, success: bool) -> None:
        self.loadFinished.emit(root, success)
