"""Qt-aware facade that bridges the CLI backend to the GUI layer."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Set, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

from .. import app as backend
from ..errors import IPhotoError
from ..models.album import Album
from .background_task_manager import BackgroundTaskManager
from .services import AlbumMetadataService, AssetImportService, AssetMoveService
from .ui.tasks.scanner_worker import ScannerSignals, ScannerWorker

if TYPE_CHECKING:
    from ..library.manager import LibraryManager
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
        self._pending_index_announcements: Set[Path] = set()
        self._library_manager: Optional["LibraryManager"] = None
        self._scanner_worker: Optional[ScannerWorker] = None
        self._scan_pending = False
        self._task_manager = BackgroundTaskManager(
            pause_watcher=self._pause_library_watcher,
            resume_watcher=self._resume_library_watcher,
            parent=self,
        )

        from .ui.models.asset_list_model import AssetListModel

        self._asset_list_model = AssetListModel(self)
        self._asset_list_model.loadProgress.connect(self._on_model_load_progress)
        self._asset_list_model.loadFinished.connect(self._on_model_load_finished)

        self._metadata_service = AlbumMetadataService(
            asset_list_model=self._asset_list_model,
            current_album_getter=lambda: self._current_album,
            library_manager_getter=self._get_library_manager,
            refresh_view=self._refresh_view,
            parent=self,
        )
        self._metadata_service.errorRaised.connect(self.errorRaised.emit)

        self._import_service = AssetImportService(
            task_manager=self._task_manager,
            current_album_root=self._current_album_root,
            refresh_callback=self._handle_import_refresh,
            metadata_service=self._metadata_service,
            parent=self,
        )
        self._import_service.errorRaised.connect(self.errorRaised.emit)

        self._move_service = AssetMoveService(
            task_manager=self._task_manager,
            asset_list_model=self._asset_list_model,
            current_album_getter=lambda: self._current_album,
            parent=self,
        )
        self._move_service.errorRaised.connect(self.errorRaised.emit)

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

    @property
    def import_service(self) -> AssetImportService:
        """Expose the import service so controllers can observe its signals."""

        return self._import_service

    @property
    def move_service(self) -> AssetMoveService:
        """Expose the move service so controllers can observe its signals."""

        return self._move_service

    @property
    def metadata_service(self) -> AlbumMetadataService:
        """Provide access to the manifest service for advanced controllers."""

        return self._metadata_service

    def open_album(self, root: Path) -> Optional[Album]:
        """Open *root* and trigger background work as needed."""

        try:
            # ``backend.open_album`` guarantees that the on-disk caches (index
            # and links) exist before returning.  The GUI model relies on
            # ``index.jsonl`` being present immediately after opening so that
            # the background asset loader can populate rows without having to
            # wait for an asynchronous rescan to finish.  Falling back to the
            # plain ``Album.open`` left a window where the tests would observe
            # a missing index file which in turn kept the models empty.
            album = backend.open_album(root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return None

        self._current_album = album
        album_root = album.root
        self._asset_list_model.prepare_for_album(album_root)
        self.albumOpened.emit(album_root)

        self._restart_asset_load(album_root, announce_index=True)
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

        album = self._require_album()
        if album is None:
            self.scanFinished.emit(None, False)
            return

        if self._scanner_worker is not None:
            self._scanner_worker.cancel()
            self._scan_pending = True
            return

        include = album.manifest.get("filters", {}).get("include", backend.DEFAULT_INCLUDE)
        exclude = album.manifest.get("filters", {}).get("exclude", backend.DEFAULT_EXCLUDE)

        # The signal container is intentionally created without a Qt parent so that it
        # outlives the facade instance for the duration of the background task.  Qt will
        # destroy child ``QObject`` instances as soon as their parent gets deleted, which
        # can easily happen in the tests where the facade goes out of scope while the
        # worker thread is still running.  Once that happens emitting any signal would
        # raise ``RuntimeError: Signal source has been deleted`` and the scan would abort
        # before writing the index.  By keeping the signals parent-less we control the
        # lifetime explicitly and dispose of them once the worker finishes.
        signals = ScannerSignals()
        signals.progressUpdated.connect(self.scanProgress.emit)
        worker = ScannerWorker(album.root, include, exclude, signals)
        self._scanner_worker = worker
        self._scan_pending = False
        self._task_manager.submit_task(
            task_id=f"scan:{album.root}",
            worker=worker,
            progress=signals.progressUpdated,
            finished=signals.finished,
            error=signals.error,
            pause_watcher=False,
            on_finished=lambda root, rows: self._on_scan_finished(worker, root, rows),
            on_error=self._on_scan_error,
            result_payload=lambda root, rows: rows,
        )

    def _pause_library_watcher(self) -> None:
        """Suspend the filesystem watcher while an internal move is in flight.

        The :class:`~iPhoto.library.manager.LibraryManager` uses a
        :class:`QFileSystemWatcher` to mirror external edits into the UI.  When a
        move originates from the application itself we want to prevent those
        notifications from immediately bouncing the gallery back to an empty
        placeholder state.  Pausing the watcher here lets the controller shield
        the UI from transient churn while the background worker performs the
        actual filesystem operations.
        """

        if self._library_manager is not None:
            self._library_manager.pause_watcher()

    def _resume_library_watcher(self) -> None:
        """Re-enable filesystem monitoring after internal operations finish.

        Resuming the watcher is slightly delayed to give the operating system a
        chance to consolidate any outstanding notifications.  Without the delay
        the watcher could fire immediately with stale events that pre-date the
        move finishing, which would put us back into the disruptive reload
        cycle the pause is trying to avoid.
        """

        if self._library_manager is not None:
            QTimer.singleShot(500, self._library_manager.resume_watcher)

    def is_performing_background_operation(self) -> bool:
        """Return ``True`` while imports or moves are still running."""

        return self._task_manager.has_watcher_blocking_tasks()

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
        self._restart_asset_load(album.root)
        return [group.__dict__ for group in groups]

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------
    def set_cover(self, rel: str) -> bool:
        """Set the album cover to *rel* and persist the manifest."""

        album = self._require_album()
        if album is None:
            return False
        return self._metadata_service.set_album_cover(album, rel)

    def bind_library(self, library: "LibraryManager") -> None:
        """Remember the library manager so static collections stay in sync."""

        self._library_manager = library

    def import_files(
        self,
        sources: Iterable[Path],
        *,
        destination: Optional[Path] = None,
        mark_featured: bool = False,
    ) -> None:
        """Import *sources* asynchronously and refresh the destination album.

        The heavy lifting is delegated to :class:`AssetImportService`, which
        performs all validation, queueing, and result reporting.  Keeping the
        workflow in a dedicated service allows the facade to remain a thin
        coordinator that merely forwards the request.
        """

        self._import_service.import_files(
            sources,
            destination=destination,
            mark_featured=mark_featured,
        )

    def move_assets(self, sources: Iterable[Path], destination: Path) -> None:
        """Move *sources* into *destination* and refresh the relevant albums."""

        self._move_service.move_assets(sources, destination)

    def toggle_featured(self, ref: str) -> bool:
        """Toggle *ref* in the active album and mirror the change in the library."""

        album = self._require_album()
        if album is None or not ref:
            return False

        return self._metadata_service.toggle_featured(album, ref)

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------
    def _require_album(self) -> Optional[Album]:
        if self._current_album is None:
            self.errorRaised.emit("No album is currently open.")
            return None
        return self._current_album

    def _on_scan_finished(
        self,
        worker: ScannerWorker,
        root: Path,
        rows: List[dict],
    ) -> None:
        if self._scanner_worker is not worker or root != worker.root:
            return

        cancelled = worker.cancelled
        failed = worker.failed

        if cancelled:
            self.scanFinished.emit(root, True)
        elif failed:
            self.scanFinished.emit(root, False)
        else:
            try:
                backend.IndexStore(root).write_rows(rows)
                backend._ensure_links(root, rows)
            except IPhotoError as exc:
                self.errorRaised.emit(str(exc))
                self.scanFinished.emit(root, False)
            else:
                self.indexUpdated.emit(root)
                self.linksUpdated.emit(root)
                if self._current_album and self._current_album.root == root:
                    self._restart_asset_load(root)
                self.scanFinished.emit(root, True)

        should_restart = self._scan_pending
        self._cleanup_scan_worker()

        if should_restart:
            QTimer.singleShot(0, self.rescan_current_async)

    def _on_scan_error(self, root: Path, message: str) -> None:
        worker = self._scanner_worker
        if worker is None or root != worker.root:
            return

        self.errorRaised.emit(message)
        self.scanFinished.emit(root, False)

        should_restart = self._scan_pending
        self._cleanup_scan_worker()

        if should_restart:
            QTimer.singleShot(0, self.rescan_current_async)

    def _cleanup_scan_worker(self) -> None:
        self._scanner_worker = None
        self._scan_pending = False

    def _handle_import_refresh(self, root: Path) -> None:
        """Announce index updates after a successful import."""

        self.indexUpdated.emit(root)
        self.linksUpdated.emit(root)
        self._restart_asset_load(root)

    def _refresh_view(self, root: Path) -> None:
        """Reload *root* so UI models pick up the latest manifest changes."""

        try:
            refreshed = Album.open(root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return

        self._current_album = refreshed
        refreshed_root = refreshed.root
        self._asset_list_model.prepare_for_album(refreshed_root)
        self.albumOpened.emit(refreshed_root)
        self._restart_asset_load(refreshed_root)

    def _current_album_root(self) -> Optional[Path]:
        """Return the filesystem root of the active album, if any."""

        if self._current_album is None:
            return None
        return self._current_album.root

    def _get_library_manager(self) -> Optional["LibraryManager"]:
        """Expose the bound library manager for service collaborators."""

        return self._library_manager

    def _restart_asset_load(self, root: Path, *, announce_index: bool = False) -> None:
        if not (self._current_album and self._current_album.root == root):
            return
        if announce_index:
            self._pending_index_announcements.add(root)
        self.loadStarted.emit(root)
        if self._asset_list_model.populate_from_cache():
            return
        self._asset_list_model.start_load()

    def _on_model_load_progress(self, root: Path, current: int, total: int) -> None:
        self.loadProgress.emit(root, current, total)

    def _on_model_load_finished(self, root: Path, success: bool) -> None:
        self.loadFinished.emit(root, success)
        if root in self._pending_index_announcements:
            self._pending_index_announcements.discard(root)
            if success:
                self.indexUpdated.emit(root)
                self.linksUpdated.emit(root)
