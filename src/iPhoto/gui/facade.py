"""Qt-aware facade that bridges the CLI backend to the GUI layer."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

from .. import app as backend
from ..errors import IPhotoError
from ..models.album import Album
from .background_task_manager import BackgroundTaskManager
from .ui.tasks.import_worker import ImportSignals, ImportWorker
from .ui.tasks.move_worker import MoveSignals, MoveWorker
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
    importStarted = Signal(object)
    importProgress = Signal(object, int, int)
    importFinished = Signal(object, bool, str)
    moveStarted = Signal(object, object)
    moveProgress = Signal(object, int, int)
    moveFinished = Signal(object, object, bool, str)

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
        album.set_cover(rel)
        return self._save_manifest(album)

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

        Parameters
        ----------
        sources:
            Candidate filesystem paths supplied by the caller.  Paths are
            normalised before being copied so duplicates and non-files are
            discarded.
        destination:
            Optional album directory that should receive the imported media.
            When omitted the currently open album is used.
        mark_featured:
            When ``True`` the facade will flag the imported files as featured
            once the worker reports success.  This is used for the Favorites
            collection.

        Notes
        -----
        The heavy lifting (filesystem copies and the follow-up rescan) runs in
        a worker thread so the UI can provide feedback via
        :class:`QProgressBar` updates.  Progress and completion information is
        surfaced through the :attr:`importStarted`, :attr:`importProgress`, and
        :attr:`importFinished` signals.
        """

        normalized = self._normalise_sources(sources)
        if not normalized:
            target_root = self._resolve_import_destination(destination)
            if target_root is not None:
                self.importFinished.emit(
                    target_root,
                    False,
                    "No files were imported.",
                )
            return

        target_root = self._resolve_import_destination(destination)
        if target_root is None:
            return

        signals = ImportSignals()
        signals.started.connect(self.importStarted.emit)
        signals.progress.connect(self.importProgress.emit)
        worker = ImportWorker(normalized, target_root, self._copy_into_album, signals)

        self._task_manager.submit_task(
            task_id=f"import:{target_root}",
            worker=worker,
            started=signals.started,
            progress=signals.progress,
            finished=signals.finished,
            error=signals.error,
            pause_watcher=True,
            on_finished=lambda root, imported, rescan_ok: self._on_import_finished(
                root,
                imported,
                rescan_ok,
                mark_featured,
            ),
            on_error=self.errorRaised.emit,
            result_payload=lambda root, imported, rescan_ok: imported,
        )

    def move_assets(self, sources: Iterable[Path], destination: Path) -> None:
        """Move *sources* into *destination* and refresh the relevant albums."""

        album = self._require_album()
        if album is None:
            # The controller pauses the watcher before invoking this method when
            # it intends to schedule a move.  If no album is open we have to
            # resume the watcher immediately because no worker will be queued to
            # do it on our behalf.
            self._asset_list_model.rollback_pending_moves()
            return
        source_root = album.root

        try:
            destination_root = Path(destination).resolve()
        except OSError as exc:
            self.errorRaised.emit(f"Invalid destination: {exc}")
            self._asset_list_model.rollback_pending_moves()
            return

        if not destination_root.exists() or not destination_root.is_dir():
            self.errorRaised.emit(
                f"Move destination is not a directory: {destination_root}"
            )
            self._asset_list_model.rollback_pending_moves()
            return

        if destination_root == source_root:
            self.moveFinished.emit(
                source_root,
                destination_root,
                False,
                "Files are already located in this album.",
            )
            self._asset_list_model.rollback_pending_moves()
            return

        normalized: list[Path] = []
        seen: set[Path] = set()
        for raw_path in sources:
            candidate = Path(raw_path)
            try:
                resolved = candidate.resolve()
            except OSError as exc:
                self.errorRaised.emit(f"Could not resolve '{candidate}': {exc}")
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.exists():
                self.errorRaised.emit(f"File not found: {resolved}")
                continue
            if resolved.is_dir():
                self.errorRaised.emit(
                    f"Skipping directory move attempt: {resolved.name}"
                )
                continue
            try:
                resolved.relative_to(source_root)
            except ValueError:
                self.errorRaised.emit(
                    f"Path '{resolved}' is not inside the active album."
                )
                continue
            normalized.append(resolved)

        if not normalized:
            self.moveFinished.emit(
                source_root,
                destination_root,
                False,
                "No valid files were selected for moving.",
            )
            self._asset_list_model.rollback_pending_moves()
            return

        signals = MoveSignals()
        signals.started.connect(self.moveStarted.emit)
        signals.progress.connect(self.moveProgress.emit)

        worker = MoveWorker(normalized, source_root, destination_root, signals)
        self._task_manager.submit_task(
            task_id=f"move:{source_root}->{destination_root}",
            worker=worker,
            started=signals.started,
            progress=signals.progress,
            finished=signals.finished,
            error=signals.error,
            pause_watcher=True,
            on_finished=lambda src, dest, moved, source_ok, destination_ok, *, move_worker=worker: self._on_move_finished(
                src,
                dest,
                moved,
                source_ok,
                destination_ok,
                move_worker,
            ),
            on_error=self.errorRaised.emit,
            result_payload=lambda src, dest, moved, *_: moved,
        )

    def toggle_featured(self, ref: str) -> bool:
        """Toggle *ref* in the active album and mirror the change in the library."""

        album = self._require_album()
        if album is None or not ref:
            return False

        featured = album.manifest.setdefault("featured", [])
        was_featured = ref in featured
        desired_state = not was_featured

        library_root: Optional[Path] = None
        if self._library_manager is not None:
            library_root = self._library_manager.root()

        root_album: Optional[Album] = None
        root_ref: Optional[str] = None
        if (
            library_root is not None
            and library_root != album.root
        ):
            try:
                # ``ref`` is relative to the currently open album.  Convert it
                # to a fully-qualified path before deriving the library-level
                # relative path used by the global manifest.
                absolute_asset = (album.root / ref).resolve()
                root_relative = absolute_asset.relative_to(library_root.resolve())
            except (OSError, ValueError):
                root_ref = None
            else:
                root_ref = root_relative.as_posix()
                try:
                    # Re-open the library root manifest on demand so updates do
                    # not disturb the album the user is currently browsing.
                    root_album = Album.open(library_root)
                except IPhotoError as exc:
                    self.errorRaised.emit(str(exc))
                    return was_featured

        if desired_state:
            album.add_featured(ref)
            if root_album is not None and root_ref is not None:
                root_album.add_featured(root_ref)
        else:
            album.remove_featured(ref)
            if root_album is not None and root_ref is not None:
                root_album.remove_featured(root_ref)

        current_saved = self._save_manifest(album, reload_view=False)
        root_saved = True
        if root_album is not None and root_ref is not None:
            root_saved = self._save_manifest(root_album, reload_view=False)

        if current_saved and root_saved:
            self._asset_list_model.update_featured_status(ref, desired_state)
            return desired_state

        if desired_state:
            album.remove_featured(ref)
            if root_album is not None and root_ref is not None:
                root_album.remove_featured(root_ref)
        else:
            album.add_featured(ref)
            if root_album is not None and root_ref is not None:
                root_album.add_featured(root_ref)
        return was_featured

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------
    def _normalise_sources(self, sources: Iterable[Path]) -> List[Path]:
        """Return a deduplicated list of candidate source files.

        The helper expands user directories, resolves symlinks where possible,
        and filters out non-files so downstream logic only needs to handle
        regular files.  Missing entries are ignored silently; the caller is
        responsible for surface-level validation before invoking the facade.
        """

        normalized: List[Path] = []
        seen: set[Path] = set()
        for candidate in sources:
            try:
                expanded = Path(candidate).expanduser()
            except TypeError:
                continue
            try:
                resolved = expanded.resolve()
            except OSError:
                resolved = expanded
            if resolved in seen:
                continue
            if not resolved.exists() or not resolved.is_file():
                continue
            seen.add(resolved)
            normalized.append(resolved)
        return normalized

    def _resolve_import_destination(self, destination: Optional[Path]) -> Optional[Path]:
        """Return a writable album root for incoming imports."""

        if destination is not None:
            try:
                target = Path(destination).expanduser().resolve()
            except OSError as exc:
                self.errorRaised.emit(f"Import destination is not accessible: {exc}")
                return None
        else:
            album = self._require_album()
            if album is None:
                return None
            target = album.root

        if not target.exists() or not target.is_dir():
            self.errorRaised.emit(f"Import destination is not a directory: {target}")
            return None
        return target

    def _copy_into_album(self, source: Path, destination: Path) -> Path:
        """Copy *source* into *destination*, avoiding name collisions."""

        base_name = source.name
        target = destination / base_name
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = destination / f"{stem} ({counter}){suffix}"
            counter += 1
        shutil.copy2(source, target)
        return target.resolve()

    def _ensure_featured_entries(self, root: Path, imported: List[Path]) -> None:
        """Append the imported files to the album's ``featured`` list."""

        album: Optional[Album]
        if self._current_album is not None and self._current_album.root == root:
            album = self._current_album
        else:
            try:
                album = Album.open(root)
            except IPhotoError as exc:
                self.errorRaised.emit(str(exc))
                return

        if album is None:
            return

        updated = False
        for path in imported:
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            album.add_featured(rel)
            updated = True

        if not updated:
            return

        saved = self._save_manifest(album, reload_view=False)
        if not saved:
            return

    def _on_import_finished(
        self,
        root: Path,
        imported: List[Path],
        rescan_succeeded: bool,
        mark_featured: bool,
    ) -> None:
        """Handle completion of an asynchronous import operation."""

        success = bool(imported) and rescan_succeeded

        if mark_featured and imported:
            self._ensure_featured_entries(root, imported)

        if rescan_succeeded and imported:
            self.indexUpdated.emit(root)
            self.linksUpdated.emit(root)
            self._restart_asset_load(root)

        if imported:
            label = "file" if len(imported) == 1 else "files"
            if rescan_succeeded:
                message = f"Imported {len(imported)} {label}."
            else:
                message = (
                    f"Imported {len(imported)} {label}, but refreshing the album failed."
                )
        else:
            message = "No files were imported."

        self.importFinished.emit(root, success, message)

    def _on_move_finished(
        self,
        source_root: Path,
        destination_root: Path,
        moved: List[Tuple[Path, Path]],
        source_ok: bool,
        destination_ok: bool,
        worker: MoveWorker,
    ) -> None:
        """Handle completion of an asynchronous move operation."""

        moved_pairs = [(Path(src), Path(dst)) for src, dst in moved]

        if worker.cancelled:
            self._asset_list_model.rollback_pending_moves()
            self.moveFinished.emit(
                source_root,
                destination_root,
                False,
                "Move cancelled.",
            )
            return

        success = bool(moved_pairs) and source_ok and destination_ok

        if moved_pairs:
            self._asset_list_model.finalise_move_results(moved_pairs)
        if self._asset_list_model.has_pending_move_placeholders():
            self._asset_list_model.rollback_pending_moves()

        # ``indexUpdated`` and ``linksUpdated`` intentionally remain quiet
        # here.  The gallery already reflects the optimistic in-memory
        # updates performed before the worker started.  Re-emitting the
        # legacy refresh signals would re-trigger the heavy album reload
        # logic we are trying to sidestep, causing visible flicker in "All
        # Photos".  Controllers that need to react to the move can listen to
        # :attr:`moveFinished` directly.

        if not moved_pairs:
            message = "No files were moved."
        else:
            label = "file" if len(moved_pairs) == 1 else "files"
            if source_ok and destination_ok:
                message = f"Moved {len(moved_pairs)} {label}."
            elif source_ok or destination_ok:
                message = (
                    f"Moved {len(moved_pairs)} {label}, but refreshing one album failed."
                )
            else:
                message = (
                    f"Moved {len(moved_pairs)} {label}, but refreshing both albums failed."
                )

        self.moveFinished.emit(source_root, destination_root, success, message)

    def _save_manifest(self, album: Album, *, reload_view: bool = True) -> bool:
        manager = self._library_manager
        if manager is not None:
            # Guard the library watcher before writing the manifest.  Without
            # this the ``QFileSystemWatcher`` would observe our own save
            # operation and interpret it as an external change, which in turn
            # kicks off a disruptive album reload.
            manager.pause_watcher()
        try:
            album.save()
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return False
        finally:
            if manager is not None:
                # Resume the watcher on the next turn of the event loop.  A
                # short delay gives the operating system time to coalesce its
                # own notifications so we avoid receiving a stale change as
                # soon as we re-enable monitoring.
                QTimer.singleShot(250, manager.resume_watcher)
        if reload_view:
            # Reload to ensure any concurrent edits are picked up.
            self._current_album = Album.open(album.root)
            refreshed_root = self._current_album.root
            self._asset_list_model.prepare_for_album(refreshed_root)
            self.albumOpened.emit(refreshed_root)
            self._restart_asset_load(refreshed_root)
        return True

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
