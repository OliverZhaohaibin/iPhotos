"""Qt-aware facade that bridges the CLI backend to the GUI layer."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Set, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal, Slot  # ⬅️ 加入 Slot

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

    # ===== GUI-facing signals =====
    albumOpened = Signal(Path)
    indexUpdated = Signal(Path)
    linksUpdated = Signal(Path)
    errorRaised = Signal(str)

    scanProgress = Signal(Path, int, int)
    scanFinished = Signal(Path, bool)

    loadStarted = Signal(Path)
    loadProgress = Signal(Path, int, int)
    loadFinished = Signal(Path, bool)

    def __init__(self) -> None:
        super().__init__()
        print("[FACADE] AppFacade.__init__ start")  # 插桩：确认新版注入

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
        # ✅ 用 _safe_connect 包装，打印每条连接
        self._safe_connect(
            self._asset_list_model.loadProgress,
            self._on_model_load_progress,
            tag="asset_list_model.loadProgress -> _on_model_load_progress",
        )
        self._safe_connect(
            self._asset_list_model.loadFinished,
            self._on_model_load_finished,
            tag="asset_list_model.loadFinished -> _on_model_load_finished",
        )

        self._metadata_service = AlbumMetadataService(
            asset_list_model=self._asset_list_model,
            current_album_getter=lambda: self._current_album,
            library_manager_getter=self._get_library_manager,
            refresh_view=self._refresh_view,
            parent=self,
        )
        # ❌ 不要 .connect(self.errorRaised.emit)
        self._safe_connect(
            self._metadata_service.errorRaised,
            self._on_service_error,
            tag="metadata_service.errorRaised -> _on_service_error",
        )

        self._import_service = AssetImportService(
            task_manager=self._task_manager,
            current_album_root=self._current_album_root,
            refresh_callback=self._handle_import_refresh,
            metadata_service=self._metadata_service,
            parent=self,
        )
        self._safe_connect(
            self._import_service.errorRaised,
            self._on_service_error,
            tag="import_service.errorRaised -> _on_service_error",
        )

        self._move_service = AssetMoveService(
            task_manager=self._task_manager,
            asset_list_model=self._asset_list_model,
            current_album_getter=lambda: self._current_album,
            parent=self,
        )
        self._safe_connect(
            self._move_service.errorRaised,
            self._on_service_error,
            tag="move_service.errorRaised -> _on_service_error",
        )

        print("[FACADE] AppFacade.__init__ done")

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
        print(f"[FACADE] open_album({root})")
        try:
            album = backend.open_album(root)
        except IPhotoError as exc:
            print(f"[FACADE][ERROR] open_album: {exc}")
            self.errorRaised.emit(str(exc))
            return None

        self._current_album = album
        album_root = album.root
        print(f"[FACADE] open_album: album_root={album_root}")
        self._asset_list_model.prepare_for_album(album_root)
        self.albumOpened.emit(album_root)

        self._restart_asset_load(album_root, announce_index=True)
        return album

    def rescan_current(self) -> List[dict]:
        """Rescan the active album and emit ``indexUpdated`` when done."""
        print("[FACADE] rescan_current()")
        album = self._require_album()
        if album is None:
            print("[FACADE] rescan_current: no album")
            return []
        try:
            rows = backend.rescan(album.root)
        except IPhotoError as exc:
            print(f"[FACADE][ERROR] rescan_current: {exc}")
            self.errorRaised.emit(str(exc))
            return []
        print("[FACADE] rescan_current: emitting indexUpdated/linksUpdated")
        self.indexUpdated.emit(album.root)
        self.linksUpdated.emit(album.root)
        self._restart_asset_load(album.root)
        return rows

    def rescan_current_async(self) -> None:
        """Start a background rescan for the active album."""
        print("[FACADE] rescan_current_async()")
        album = self._require_album()
        if album is None:
            print("[FACADE] rescan_current_async: no album")
            self.scanFinished.emit(None, False)
            return

        if self._scanner_worker is not None:
            print("[FACADE] rescan_current_async: cancel existing worker, set pending")
            self._scanner_worker.cancel()
            self._scan_pending = True
            return

        include = album.manifest.get("filters", {}).get("include", backend.DEFAULT_INCLUDE)
        exclude = album.manifest.get("filters", {}).get("exclude", backend.DEFAULT_EXCLUDE)
        print(f"[FACADE] rescan_current_async: include={include}, exclude={exclude}")

        # 注意：ScannerSignals 不要连到 .emit
        signals = ScannerSignals()
        self._safe_connect(
            signals.progressUpdated,
            self._on_scan_progress,
            tag="scanner.signals.progressUpdated -> _on_scan_progress",
        )
        worker = ScannerWorker(album.root, include, exclude, signals)
        self._scanner_worker = worker
        self._scan_pending = False

        print("[FACADE] rescan_current_async: submit_task")
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
        """Suspend the filesystem watcher while an internal move is in flight."""
        print("[FACADE] _pause_library_watcher()")
        if self._library_manager is not None:
            self._library_manager.pause_watcher()

    def _resume_library_watcher(self) -> None:
        """Re-enable filesystem monitoring after internal operations finish."""
        print("[FACADE] _resume_library_watcher()")
        if self._library_manager is not None:
            QTimer.singleShot(500, self._library_manager.resume_watcher)

    def is_performing_background_operation(self) -> bool:
        """Return ``True`` while imports or moves are still running."""
        status = self._task_manager.has_watcher_blocking_tasks()
        print(f"[FACADE] is_performing_background_operation() -> {status}")
        return status

    def pair_live_current(self) -> List[dict]:
        """Rebuild Live Photo pairings for the active album."""
        print("[FACADE] pair_live_current()")
        album = self._require_album()
        if album is None:
            print("[FACADE] pair_live_current: no album")
            return []
        try:
            groups = backend.pair(album.root)
        except IPhotoError as exc:
            print(f"[FACADE][ERROR] pair_live_current: {exc}")
            self.errorRaised.emit(str(exc))
            return []
        print("[FACADE] pair_live_current: emitting linksUpdated + reload")
        self.linksUpdated.emit(album.root)
        self._restart_asset_load(album.root)
        return [group.__dict__ for group in groups]

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------
    def set_cover(self, rel: str) -> bool:
        """Set the album cover to *rel* and persist the manifest."""
        print(f"[FACADE] set_cover({rel})")
        album = self._require_album()
        if album is None:
            print("[FACADE] set_cover: no album")
            return False
        return self._metadata_service.set_album_cover(album, rel)

    def bind_library(self, library: "LibraryManager") -> None:
        """Remember the library manager so static collections stay in sync."""
        print("[FACADE] bind_library()")
        self._library_manager = library

    def import_files(
        self,
        sources: Iterable[Path],
        *,
        destination: Optional[Path] = None,
        mark_featured: bool = False,
    ) -> None:
        """Import *sources* asynchronously and refresh the destination album."""
        print(f"[FACADE] import_files(sources={len(list(sources)) if sources else 0}, "
              f"destination={destination}, mark_featured={mark_featured})")
        self._import_service.import_files(
            sources,
            destination=destination,
            mark_featured=mark_featured,
        )

    def move_assets(self, sources: Iterable[Path], destination: Path) -> None:
        """Move *sources* into *destination* and refresh the relevant albums."""
        print(f"[FACADE] move_assets({len(list(sources)) if sources else 0} items -> {destination})")
        self._move_service.move_assets(sources, destination)

    def toggle_featured(self, ref: str) -> bool:
        """Toggle *ref* in the active album and mirror the change in the library."""
        print(f"[FACADE] toggle_featured({ref})")
        album = self._require_album()
        if album is None or not ref:
            print("[FACADE] toggle_featured: no album or empty ref")
            return False

        return self._metadata_service.toggle_featured(album, ref)

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------
    def _require_album(self) -> Optional[Album]:
        if self._current_album is None:
            print("[FACADE] _require_album: None")
            self.errorRaised.emit("No album is currently open.")
            return None
        return self._current_album

    def _on_scan_finished(
        self,
        worker: ScannerWorker,
        root: Path,
        rows: List[dict],
    ) -> None:
        print(f"[FACADE] _on_scan_finished(root={root}, rows={len(rows)})")
        if self._scanner_worker is not worker or root != worker.root:
            print("[FACADE] _on_scan_finished: stale worker or mismatched root, ignore")
            return

        cancelled = worker.cancelled
        failed = worker.failed
        print(f"[FACADE] _on_scan_finished: cancelled={cancelled}, failed={failed}")

        if cancelled:
            print("[FACADE] _on_scan_finished: emit scanFinished(cancelled=True)")
            self.scanFinished.emit(root, True)
        elif failed:
            print("[FACADE] _on_scan_finished: emit scanFinished(success=False)")
            self.scanFinished.emit(root, False)
        else:
            try:
                print("[FACADE] _on_scan_finished: write index & ensure links")
                backend.IndexStore(root).write_rows(rows)
                backend._ensure_links(root, rows)
            except IPhotoError as exc:
                print(f"[FACADE][ERROR] _on_scan_finished: {exc}")
                self.errorRaised.emit(str(exc))
                self.scanFinished.emit(root, False)
            else:
                print("[FACADE] _on_scan_finished: emit indexUpdated/linksUpdated + restart asset load")
                self.indexUpdated.emit(root)
                self.linksUpdated.emit(root)
                if self._current_album and self._current_album.root == root:
                    self._restart_asset_load(root)
                self.scanFinished.emit(root, True)

        should_restart = self._scan_pending
        self._cleanup_scan_worker()

        if should_restart:
            print("[FACADE] _on_scan_finished: pending rescan, schedule")
            QTimer.singleShot(0, self.rescan_current_async)

    def _on_scan_error(self, root: Path, message: str) -> None:
        print(f"[FACADE] _on_scan_error(root={root}): {message}")
        worker = self._scanner_worker
        if worker is None or root != worker.root:
            print("[FACADE] _on_scan_error: stale worker or mismatched root, ignore")
            return

        self.errorRaised.emit(message)
        self.scanFinished.emit(root, False)

        should_restart = self._scan_pending
        self._cleanup_scan_worker()

        if should_restart:
            print("[FACADE] _on_scan_error: pending rescan, schedule")
            QTimer.singleShot(0, self.rescan_current_async)

    def _cleanup_scan_worker(self) -> None:
        print("[FACADE] _cleanup_scan_worker()")
        self._scanner_worker = None
        self._scan_pending = False

    def _handle_import_refresh(self, root: Path) -> None:
        """Announce index updates after a successful import."""
        print(f"[FACADE] _handle_import_refresh({root}) -> emit indexUpdated/linksUpdated + restart")
        self.indexUpdated.emit(root)
        self.linksUpdated.emit(root)
        self._restart_asset_load(root)

    def _refresh_view(self, root: Path) -> None:
        """Reload *root* so UI models pick up the latest manifest changes."""
        print(f"[FACADE] _refresh_view({root})")
        try:
            refreshed = Album.open(root)
        except IPhotoError as exc:
            print(f"[FACADE][ERROR] _refresh_view: {exc}")
            self.errorRaised.emit(str(exc))
            return

        self._current_album = refreshed
        refreshed_root = refreshed.root
        print(f"[FACADE] _refresh_view: refreshed_root={refreshed_root}")
        self._asset_list_model.prepare_for_album(refreshed_root)
        self.albumOpened.emit(refreshed_root)
        self._restart_asset_load(refreshed_root)

    def _current_album_root(self) -> Optional[Path]:
        """Return the filesystem root of the active album, if any."""
        root = None if self._current_album is None else self._current_album.root
        print(f"[FACADE] _current_album_root() -> {root}")
        return root

    def _get_library_manager(self) -> Optional["LibraryManager"]:
        """Expose the bound library manager for service collaborators."""
        print(f"[FACADE] _get_library_manager() -> {self._library_manager}")
        return self._library_manager

    def _restart_asset_load(self, root: Path, *, announce_index: bool = False) -> None:
        print(f"[FACADE] _restart_asset_load(root={root}, announce_index={announce_index})")
        if not (self._current_album and self._current_album.root == root):
            print("[FACADE] _restart_asset_load: no current album or root mismatch, ignore")
            return
        if announce_index:
            self._pending_index_announcements.add(root)
        self.loadStarted.emit(root)
        if self._asset_list_model.populate_from_cache():
            print("[FACADE] _restart_asset_load: cache hit, skip async load")
            return
        print("[FACADE] _restart_asset_load: start async load")
        self._asset_list_model.start_load()

    # ===== Slots (统一在槽里转发/emit，避免 .connect(.emit) 触发 Nuitka patched_connect) =====

    @Slot(Path, int, int)
    def _on_model_load_progress(self, root: Path, current: int, total: int) -> None:
        print(f"[FACADE][SLOT] _on_model_load_progress(root={root}, {current}/{total})")
        self.loadProgress.emit(root, current, total)

    @Slot(Path, bool)
    def _on_model_load_finished(self, root: Path, success: bool) -> None:
        print(f"[FACADE][SLOT] _on_model_load_finished(root={root}, success={success})")
        self.loadFinished.emit(root, success)
        if root in self._pending_index_announcements:
            print("[FACADE] _on_model_load_finished: announce pending indexUpdated/linksUpdated")
            self._pending_index_announcements.discard(root)
            if success:
                self.indexUpdated.emit(root)
                self.linksUpdated.emit(root)

    @Slot(str)
    def _on_service_error(self, message: str) -> None:
        print(f"[FACADE][SLOT] _on_service_error: {message}")
        self.errorRaised.emit(message)

    @Slot(Path, int, int)
    def _on_scan_progress(self, root: Path, current: int, total: int) -> None:
        print(f"[FACADE][SLOT] _on_scan_progress(root={root}, {current}/{total})")
        self.scanProgress.emit(root, current, total)

    # ===== 安全 connect 包装，打印可调用性与失败信息，快速定位真正的“元凶” =====
    def _safe_connect(self, signal, slot, *, tag: str) -> None:
        try:
            print(f"[FACADE][CONNECT] {tag}: slot={slot!r}, callable={callable(slot)}, type={type(slot)}")
            signal.connect(slot)
        except Exception as e:
            # 打印具体是哪一条 connect 失败，便于快速定位
            print(f"[FACADE][CONNECT-ERROR] {tag}: {e}")
            raise
