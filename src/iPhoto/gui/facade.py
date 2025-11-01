"""Qt-aware facade that bridges the CLI backend to the GUI layer."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from .. import app as backend
from ..errors import AlbumOperationError, IPhotoError
from ..models.album import Album
from .background_task_manager import BackgroundTaskManager
from .services import (
    AlbumMetadataService,
    AssetImportService,
    AssetMoveService,
    LibraryUpdateService,
)

if TYPE_CHECKING:
    from ..library.manager import LibraryManager
    from .ui.models.asset_list_model import AssetListModel


class AppFacade(QObject):
    """Expose high-level album operations to the GUI layer."""

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
        self._current_album: Optional[Album] = None
        self._pending_index_announcements: Set[Path] = set()
        self._library_manager: Optional["LibraryManager"] = None
        self._restore_prompt_handler: Optional[Callable[[str], bool]] = None

        def _pause_watcher() -> None:
            """Suspend the library watcher while background tasks mutate files."""

            manager = self._library_manager
            if manager is not None:
                manager.pause_watcher()

        def _resume_watcher() -> None:
            """Resume filesystem monitoring after background work completes."""

            manager = self._library_manager
            if manager is not None:
                manager.resume_watcher()

        self._task_manager = BackgroundTaskManager(
            pause_watcher=_pause_watcher,
            resume_watcher=_resume_watcher,
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
        self._metadata_service.errorRaised.connect(self._on_service_error)

        self._library_update_service = LibraryUpdateService(
            task_manager=self._task_manager,
            current_album_getter=lambda: self._current_album,
            library_manager_getter=self._get_library_manager,
            parent=self,
        )
        self._library_update_service.scanProgress.connect(self._relay_scan_progress)
        self._library_update_service.scanFinished.connect(self._relay_scan_finished)
        self._library_update_service.indexUpdated.connect(self._relay_index_updated)
        self._library_update_service.linksUpdated.connect(self._relay_links_updated)
        self._library_update_service.assetReloadRequested.connect(
            self._on_asset_reload_requested
        )
        self._library_update_service.errorRaised.connect(self._on_service_error)

        self._import_service = AssetImportService(
            task_manager=self._task_manager,
            current_album_root=self._current_album_root,
            update_service=self._library_update_service,
            metadata_service=self._metadata_service,
            parent=self,
        )
        self._import_service.errorRaised.connect(self._on_service_error)

        self._move_service = AssetMoveService(
            task_manager=self._task_manager,
            asset_list_model=self._asset_list_model,
            current_album_getter=lambda: self._current_album,
            library_manager_getter=self._get_library_manager,
            parent=self,
        )
        self._move_service.errorRaised.connect(self._on_service_error)
        self._move_service.moveCompletedDetailed.connect(
            self._library_update_service.handle_move_operation_completed
        )

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

    @property
    def library_updates(self) -> LibraryUpdateService:
        """Expose the library update service for direct signal subscriptions."""

        return self._library_update_service

    def open_album(self, root: Path) -> Optional[Album]:
        """Open *root* and trigger background work as needed."""

        try:
            album = backend.open_album(root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return None

        self._current_album = album
        album_root = album.root
        self._asset_list_model.prepare_for_album(album_root)
        self.albumOpened.emit(album_root)

        force_reload = self._library_update_service.consume_forced_reload(album_root)
        self._restart_asset_load(
            album_root,
            announce_index=True,
            force_reload=force_reload,
        )
        return album

    def rescan_current(self) -> List[dict]:
        """Rescan the active album and emit ``indexUpdated`` when done."""

        album = self._require_album()
        if album is None:
            return []
        return self._library_update_service.rescan_album(album)

    def rescan_current_async(self) -> None:
        """Start a background rescan for the active album."""

        album = self._require_album()
        if album is None:
            return
        self._library_update_service.rescan_album_async(album)

    def is_performing_background_operation(self) -> bool:
        """Return ``True`` while imports or moves are still running."""

        return self._task_manager.has_watcher_blocking_tasks()

    def pair_live_current(self) -> List[dict]:
        """Rebuild Live Photo pairings for the active album."""

        album = self._require_album()
        if album is None:
            return []
        return self._library_update_service.pair_live(album)

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
        self._library_update_service.reset_cache()

    def register_restore_prompt(
        self, handler: Optional[Callable[[str], bool]]
    ) -> None:
        """Register *handler* to confirm restore-to-root fallbacks.

        The GUI injects :meth:`DialogController.prompt_restore_to_root` here so
        :meth:`restore_assets` can ask the user before placing an item directly
        into the Basic Library root when its original album no longer exists or
        when metadata about the intended destination has been lost.  Passing
        ``None`` disables the prompt and causes such restores to be skipped
        automatically.
        """

        self._restore_prompt_handler = handler

    def import_files(
        self,
        sources: Iterable[Path],
        *,
        destination: Optional[Path] = None,
        mark_featured: bool = False,
    ) -> None:
        """Import *sources* asynchronously and refresh the destination album."""

        self._import_service.import_files(
            sources,
            destination=destination,
            mark_featured=mark_featured,
        )

    def move_assets(self, sources: Iterable[Path], destination: Path) -> None:
        """Move *sources* into *destination* and refresh the relevant albums."""

        self._move_service.move_assets(sources, destination)

    def delete_assets(self, sources: Iterable[Path]) -> None:
        """Move *sources* into the dedicated deleted-items folder."""

        library = self._get_library_manager()
        if library is None:
            self.errorRaised.emit("Basic Library has not been configured.")
            return

        try:
            deleted_root = library.ensure_deleted_directory()
        except AlbumOperationError as exc:
            self.errorRaised.emit(str(exc))
            return

        def _normalize(path: Path) -> Path:
            """Resolve *path* for stable comparisons while tolerating I/O errors."""

            try:
                return path.resolve()
            except OSError:
                return path

        normalized: List[Path] = []
        seen: Set[str] = set()
        for raw_path in sources:
            candidate = _normalize(Path(raw_path))
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(candidate)

        if not normalized:
            return

        model = self._asset_list_model
        for still_path in list(normalized):
            metadata = model.metadata_for_absolute_path(still_path)
            if not metadata or not metadata.get("is_live"):
                continue
            motion_raw = metadata.get("live_motion_abs")
            if not motion_raw:
                continue
            motion_path = _normalize(Path(str(motion_raw)))
            motion_key = str(motion_path)
            if motion_key not in seen:
                seen.add(motion_key)
                normalized.append(motion_path)

        self._move_service.move_assets(normalized, deleted_root, operation="delete")

    def restore_assets(self, sources: Iterable[Path]) -> bool:
        """Return ``True`` when at least one trashed asset restore is scheduled.

        The caller uses the boolean result to decide whether restore-specific
        UI affordances (such as the transient overlay toast) should be
        displayed.  Returning ``False`` indicates that no work was queued—either
        because the selection was empty, metadata was missing, or the user
        declined all fallbacks.
        """

        library = self._get_library_manager()
        if library is None:
            self.errorRaised.emit("Basic Library has not been configured.")
            return False

        library_root = library.root()
        if library_root is None:
            self.errorRaised.emit("Basic Library has not been configured.")
            return False

        trash_root = library.deleted_directory()
        if trash_root is None:
            self.errorRaised.emit("Recently Deleted folder is unavailable.")
            return False

        def _normalize(path: Path) -> Path:
            """Resolve *path* for comparisons while tolerating resolution errors."""

            try:
                return path.resolve()
            except OSError:
                return path

        normalized: List[Path] = []
        seen: Set[str] = set()
        for raw_path in sources:
            candidate = _normalize(Path(raw_path))
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if not candidate.exists():
                self.errorRaised.emit(f"File not found: {candidate}")
                continue
            try:
                candidate.relative_to(trash_root)
            except ValueError:
                self.errorRaised.emit(
                    f"Selection is outside Recently Deleted: {candidate}"
                )
                continue
            normalized.append(candidate)

        if not normalized:
            return False

        model = self._asset_list_model
        for still_path in list(normalized):
            metadata = model.metadata_for_absolute_path(still_path)
            if not metadata or not metadata.get("is_live"):
                continue
            motion_raw = metadata.get("live_motion_abs")
            if not motion_raw:
                continue
            motion_path = _normalize(Path(str(motion_raw)))
            motion_key = str(motion_path)
            if motion_key not in seen and motion_path.exists():
                seen.add(motion_key)
                try:
                    motion_path.relative_to(trash_root)
                except ValueError:
                    continue
                normalized.append(motion_path)

        index_rows = list(backend.IndexStore(trash_root).read_all())
        row_lookup: Dict[str, dict] = {}
        for row in index_rows:
            if not isinstance(row, dict):
                continue
            rel_value = row.get("rel")
            if not isinstance(rel_value, str):
                continue
            abs_candidate = trash_root / rel_value
            key = str(_normalize(abs_candidate))
            row_lookup[key] = row

        grouped: Dict[Path, List[Path]] = defaultdict(list)
        for path in normalized:
            try:
                key = str(_normalize(path))
                row = row_lookup.get(key)
                if not row:
                    raise LookupError("metadata unavailable")
                destination_root = self._determine_restore_destination(
                    row=row,
                    library=library,
                    library_root=library_root,
                    filename=path.name,
                )
                if destination_root is None:
                    continue
                destination_root.mkdir(parents=True, exist_ok=True)
            except LookupError:
                self.errorRaised.emit(
                    f"Missing index metadata for {path.name}; skipping restore."
                )
                continue
            except OSError as exc:
                self.errorRaised.emit(
                    f"Could not prepare restore destination '{destination_root}': {exc}"
                )
                continue
            grouped[destination_root].append(path)

        if not grouped:
            return False

        scheduled_restore = False
        for destination_root, paths in grouped.items():
            self._move_service.move_assets(
                paths,
                destination_root,
                operation="restore",
            )
            scheduled_restore = True

        # ``scheduled_restore`` is returned so higher-level controllers can
        # determine whether to surface restore-specific UI feedback.  The flag
        # only flips to ``True`` after at least one worker has been queued,
        # meaning that ``False`` captures both validation failures and user
        # cancellations.
        return scheduled_restore

    def _determine_restore_destination(
        self,
        *,
        row: dict,
        library: "LibraryManager",
        library_root: Path,
        filename: str,
    ) -> Optional[Path]:
        """Return the directory that should receive a restored asset.

        The helper first attempts to honour the original relative path when the
        parent directory still exists.  Failing that, it consults the album
        identifier metadata persisted at deletion time to locate the album even
        after it has been renamed.  When the album is no longer present, the
        optional restore prompt handler decides whether the asset should fall
        back to the Basic Library root.
        """

        def _offer_restore_to_root(
            skip_reason: str,
            decline_reason: str,
        ) -> Optional[Path]:
            """Offer to restore *filename* directly into the Basic Library root.

            Restore workflows can reach this helper in two situations: when the
            original album has been removed from the library or when the metadata
            that describes the intended destination is no longer available (for
            instance because external tools manipulated the trash index).  The
            helper centralises the prompt logic so every caller consistently
            communicates why a fallback is required.
            """

            prompt = self._restore_prompt_handler
            if prompt is None:
                self.errorRaised.emit(skip_reason)
                return None
            if prompt(filename):
                return library_root
            self.errorRaised.emit(decline_reason)
            return None

        original_rel = row.get("original_rel_path")
        if isinstance(original_rel, str) and original_rel:
            candidate_path = library_root / original_rel
            try:
                candidate_path.relative_to(library_root)
            except ValueError:
                # A stale or malicious value could escape the library root; fall
                # back to the album metadata in that scenario.
                pass
            else:
                parent_dir = candidate_path.parent
                if parent_dir.exists():
                    return parent_dir

        album_id = row.get("original_album_id")
        subpath = row.get("original_album_subpath")
        if isinstance(album_id, str) and album_id and isinstance(subpath, str) and subpath:
            node = library.find_album_by_uuid(album_id)
            if node is not None:
                subpath_obj = Path(subpath)
                if subpath_obj.is_absolute() or any(part == ".." for part in subpath_obj.parts):
                    destination_root = node.path
                else:
                    destination_path = node.path / subpath_obj
                    try:
                        destination_path.relative_to(node.path)
                    except ValueError:
                        destination_root = node.path
                    else:
                        destination_root = destination_path.parent
                return destination_root

            return _offer_restore_to_root(
                skip_reason=(
                    f"Original album for {filename} no longer exists; skipping restore."
                ),
                decline_reason=(
                    f"Restore cancelled for {filename} because its original album is unavailable."
                ),
            )

        if isinstance(original_rel, str) and original_rel:
            # We only reach this point when the quick path failed because the
            # parent folder disappeared *and* we lack album metadata.  Surface a
            # clear error so the user understands why the restore could not
            # proceed automatically.
            return _offer_restore_to_root(
                skip_reason=(
                    f"Original album metadata is unavailable for {filename}; skipping restore."
                ),
                decline_reason=(
                    f"Restore cancelled for {filename} because you opted against placing it in the Basic Library root."
                ),
            )
        return _offer_restore_to_root(
            skip_reason=(
                f"Original location is unknown for {filename}; skipping restore."
            ),
            decline_reason=(
                f"Restore cancelled for {filename} because you opted against placing it in the Basic Library root."
            ),
        )

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
        force_reload = self._library_update_service.consume_forced_reload(refreshed_root)
        self._restart_asset_load(refreshed_root, force_reload=force_reload)

    def _current_album_root(self) -> Optional[Path]:
        if self._current_album is None:
            return None
        return self._current_album.root

    def _paths_equal(self, first: Path, second: Path) -> bool:
        """Return ``True`` when *first* and *second* identify the same location."""

        # Resolve both inputs where possible to neutralise redundant separators,
        # relative segments, and platform-specific quirks (for instance network
        # shares on Windows).  The legacy tests – and a few controllers – call
        # into this helper directly, so we retain the behaviour the GUI relied
        # upon prior to the service refactor.
        if first == second:
            return True

        try:
            normalised_first = first.resolve()
        except OSError:
            normalised_first = first

        try:
            normalised_second = second.resolve()
        except OSError:
            normalised_second = second

        return normalised_first == normalised_second

    def _get_library_manager(self) -> Optional["LibraryManager"]:
        return self._library_manager

    def _restart_asset_load(
        self,
        root: Path,
        *,
        announce_index: bool = False,
        force_reload: bool = False,
    ) -> None:
        if not (self._current_album and self._current_album.root == root):
            return
        if announce_index:
            self._pending_index_announcements.add(root)
        self.loadStarted.emit(root)
        if not force_reload and self._asset_list_model.populate_from_cache():
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

    @Slot(Path, Path, list, bool, bool, bool, bool)
    def _handle_move_operation_completed(
        self,
        source_root: Path,
        destination_root: Path,
        moved_pairs: list,
        source_ok: bool,
        destination_ok: bool,
        is_trash_destination: bool,
        is_restore_operation: bool,
    ) -> None:
        """Preserve the legacy private API by delegating to the new service."""

        # The library update service now owns the heavy lifting, but the tests –
        # and potentially integrations built against older versions – still
        # reach into this private helper directly.  Forward the invocation so
        # the updated design remains behaviourally compatible without
        # reintroducing the duplicated bookkeeping logic here.
        self._library_update_service.handle_move_operation_completed(
            source_root,
            destination_root,
            moved_pairs,
            source_ok,
            destination_ok,
            is_trash_destination,
            is_restore_operation,
        )

    @Slot(str)
    def _on_service_error(self, message: str) -> None:
        """Relay service-level failures through the facade-wide error signal."""

        self.errorRaised.emit(message)

    @Slot(Path, int, int)
    def _relay_scan_progress(self, root: Path, current: int, total: int) -> None:
        """Forward scan progress updates emitted by :class:`LibraryUpdateService`."""

        self.scanProgress.emit(root, current, total)

    @Slot(Path, bool)
    def _relay_scan_finished(self, root: Path, success: bool) -> None:
        """Forward scan completion events to existing facade listeners."""

        self.scanFinished.emit(root, success)

    @Slot(Path)
    def _relay_index_updated(self, root: Path) -> None:
        """Re-emit index refresh notifications for backwards compatibility."""

        self.indexUpdated.emit(root)

    @Slot(Path)
    def _relay_links_updated(self, root: Path) -> None:
        """Re-emit pairing refresh notifications for backwards compatibility."""

        self.linksUpdated.emit(root)

    @Slot(Path, bool, bool)
    def _on_asset_reload_requested(
        self,
        root: Path,
        announce_index: bool,
        force_reload: bool,
    ) -> None:
        """Trigger an asset reload in response to library update notifications."""

        self._restart_asset_load(
            root,
            announce_index=announce_index,
            force_reload=force_reload,
        )


__all__ = ["AppFacade"]
