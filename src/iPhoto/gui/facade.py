"""Qt-aware facade that bridges the CLI backend to the GUI layer."""

from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .. import app as backend
from ..errors import AlbumOperationError, IPhotoError
from ..models.album import Album
from .background_task_manager import BackgroundTaskManager
from .services import AlbumMetadataService, AssetImportService, AssetMoveService
from .ui.tasks.scanner_worker import ScannerSignals, ScannerWorker
from .ui.tasks.rescan_worker import RescanSignals, RescanWorker

if TYPE_CHECKING:
    from ..library.manager import LibraryManager
    from .ui.models.asset_list_model import AssetListModel


class AppFacade(QObject):
    """Expose high-level album operations to the GUI layer."""

    # Qt signal signatures use ``Path`` rather than ``object`` so that Nuitka's
    # strict type matching can verify the corresponding slots during static
    # analysis.  The application exclusively emits filesystem paths for these
    # events, therefore tightening the signature does not change behaviour but
    # avoids packaging-time ``SystemError`` exceptions.
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
        self._metadata_service.errorRaised.connect(self._on_service_error)

        self._import_service = AssetImportService(
            task_manager=self._task_manager,
            current_album_root=self._current_album_root,
            refresh_callback=self._handle_import_refresh,
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
            self._handle_move_operation_completed
        )

    @Slot(str)
    def _on_service_error(self, message: str) -> None:
        """Relay service-level failures through the facade-wide error signal.

        Nuitka performs strict validation of connected callables and rejects
        method descriptors such as :py:meth:`Signal.emit`.  A dedicated slot
        gives the compiler an explicit Python callable to link, while keeping
        the runtime behaviour identical by forwarding the original error
        message verbatim to :attr:`errorRaised`.
        """

        self.errorRaised.emit(message)

    @Slot(Path, int, int)
    def _on_scan_progress(self, root: Path, current: int, total: int) -> None:
        """Bridge worker progress updates back to the public ``scanProgress`` signal.

        The worker emits native ``ScannerSignals.progressUpdated`` events which
        Nuitka refuses to link directly to :py:meth:`Signal.emit`.  By providing
        an explicit slot we guarantee that the connection targets a callable
        object recognised by Qt's meta-object system and can therefore be
        preserved during compilation.
        """

        self.scanProgress.emit(root, current, total)

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
        signals.progressUpdated.connect(self._on_scan_progress)
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

        # Extend the deletion payload with Live Photo motion clips so that moving the still
        # automatically collects its paired ``.mov`` asset.  The list model keeps the metadata
        # for every loaded asset, allowing us to query the Live Photo role without having to
        # replicate the pairing logic here.
        model = self._asset_list_model
        # Extend the restore payload with any Live Photo motion clips that belong to the
        # selection so the pair returns to the library together.
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

    def restore_assets(self, sources: Iterable[Path]) -> None:
        """Return trashed assets in *sources* to their original albums."""

        library = self._get_library_manager()
        if library is None:
            self.errorRaised.emit("Basic Library has not been configured.")
            return

        library_root = library.root()
        if library_root is None:
            self.errorRaised.emit("Basic Library has not been configured.")
            return

        trash_root = library.deleted_directory()
        if trash_root is None:
            self.errorRaised.emit("Recently Deleted folder is unavailable.")
            return

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

        # ``grouped`` collects the restore requests keyed by their target album directory so
        # that each batch can be forwarded to the existing move service unchanged.
        grouped: Dict[Path, List[Path]] = defaultdict(list)
        for path in normalized:
            try:
                key = str(_normalize(path))
                row = row_lookup.get(key)
                if not row:
                    raise LookupError("metadata unavailable")
                original_rel = row.get("original_rel_path")
                if not isinstance(original_rel, str) or not original_rel:
                    raise KeyError("original_rel_path")
                destination_path = library_root / original_rel
                destination_root = destination_path.parent
                destination_root.mkdir(parents=True, exist_ok=True)
            except LookupError:
                self.errorRaised.emit(
                    f"Missing index metadata for {path.name}; skipping restore."
                )
                continue
            except KeyError:
                self.errorRaised.emit(
                    f"Original location is unknown for {path.name}; skipping restore."
                )
                continue
            except OSError as exc:
                self.errorRaised.emit(
                    f"Could not prepare restore destination '{destination_root}': {exc}"
                )
                continue
            grouped[destination_root].append(path)

        if not grouped:
            return

        for destination_root, paths in grouped.items():
            self._move_service.move_assets(
                paths,
                destination_root,
                operation="restore",
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

    @Slot(Path, Path, list, bool, bool, bool, bool)
    def _handle_move_operation_completed(
        self,
        source_root: Path,
        _destination_root: Path,
        moved_pairs: list,
        _source_ok: bool,
        destination_ok: bool,
        _is_trash_destination: bool,
        is_restore_operation: bool,
    ) -> None:
        """Refresh destination albums after a successful restore operation."""

        if not is_restore_operation or not destination_ok or not moved_pairs:
            return

        library = self._get_library_manager()
        if library is None:
            return

        trash_root = library.deleted_directory()
        if trash_root is None:
            return

        if not self._paths_equal(source_root, trash_root):
            # Only handle restores that originate from the managed trash
            # directory.  Standard move and delete operations follow the
            # existing code paths that rely on filesystem watcher updates.
            return

        library_root = library.root()
        library_root_normalised = (
            self._normalise_path(library_root) if library_root is not None else None
        )

        unique_album_roots: dict[str, Path] = {}
        for pair in moved_pairs:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                continue
            _, destination = pair
            album_root = Path(destination).parent
            normalised_album = self._normalise_path(album_root)
            if library_root_normalised is not None:
                try:
                    normalised_album.relative_to(library_root_normalised)
                except ValueError:
                    continue
            key = str(normalised_album)
            if key not in unique_album_roots:
                unique_album_roots[key] = normalised_album

        if not unique_album_roots:
            return

        for album_root in unique_album_roots.values():
            self._refresh_restored_album(album_root, library_root)

    def _refresh_restored_album(self, album_root: Path, library_root: Optional[Path]) -> None:
        """Queue a background rescan for *album_root* to keep the UI responsive.

        ``library_root`` is forwarded so the method can determine whether the
        "All Photos"/"Live Photos" composite view—whose dataset is rooted at
        the Basic Library directory—needs to refresh alongside the concrete
        album that received the restored files.  This avoids leaving the
        library-wide views stale after a restore that originated from those
        virtual collections.
        """

        album_root = Path(album_root)
        if not album_root.exists():
            return

        library_root_path = Path(library_root) if library_root is not None else None

        signals = RescanSignals()
        worker = RescanWorker(album_root, signals)
        task_id = self._build_restore_rescan_task_id(album_root)

        def _on_finished(path: Path, succeeded: bool) -> None:
            """Emit index updates once the rescan completes successfully."""

            if not succeeded:
                return

            self.indexUpdated.emit(path)
            self.linksUpdated.emit(path)

            if self._current_album and self._paths_equal(self._current_album.root, path):
                # ``_restart_asset_load`` expects the canonical album reference, not the
                # normalised path that may come back from the worker, to avoid needless
                # detaches of the existing selection model.
                self._restart_asset_load(self._current_album.root)
                return

            if (
                library_root_path is not None
                and self._current_album is not None
                and self._paths_equal(self._current_album.root, library_root_path)
                and self._path_is_descendant(path, library_root_path)
            ):
                # When a restore occurs while a library-scoped virtual view (such as
                # "All Photos" or "Live Photos") is active the asset model is rooted at
                # the Basic Library directory.  Refresh that dataset so recently
                # restored Live Photos immediately reappear without requiring a manual
                # rescan from the user.
                self._restart_asset_load(self._current_album.root)

        def _on_error(path: Path, message: str) -> None:
            """Relay background failures with contextual information."""

            self.errorRaised.emit(f"Failed to refresh '{path.name}': {message}")

        self._task_manager.submit_task(
            task_id=task_id,
            worker=worker,
            finished=signals.finished,
            error=signals.error,
            pause_watcher=False,
            on_finished=_on_finished,
            on_error=_on_error,
            result_payload=lambda path, succeeded: (path, succeeded),
        )

    def _build_restore_rescan_task_id(self, album_root: Path) -> str:
        """Return a stable yet unique identifier for restore-triggered rescans."""

        normalised = self._normalise_path(album_root)
        return f"restore-rescan:{normalised}:{uuid.uuid4().hex}"

    def _normalise_path(self, path: Optional[Path]) -> Path:
        """Return a consistently resolved variant of *path* for comparisons."""

        if path is None:
            raise ValueError("Cannot normalise a null path.")
        try:
            return path.resolve()
        except OSError:
            return path

    def _paths_equal(self, left: Path, right: Path) -> bool:
        """Return ``True`` when *left* and *right* refer to the same location."""

        if left == right:
            return True
        return self._normalise_path(left) == self._normalise_path(right)

    def _path_is_descendant(self, candidate: Path, ancestor: Path) -> bool:
        """Return ``True`` when *candidate* is equal to or contained within *ancestor*.

        Both paths are normalised before comparison so that symbolic links and
        platform-specific casing differences do not cause false negatives.  The
        helper tolerates resolution errors and simply reports ``False`` when the
        relationship cannot be established reliably.
        """

        try:
            candidate_norm = self._normalise_path(candidate)
            ancestor_norm = self._normalise_path(ancestor)
        except ValueError:
            return False

        if candidate_norm == ancestor_norm:
            return True

        try:
            candidate_norm.relative_to(ancestor_norm)
        except ValueError:
            return False
        return True
