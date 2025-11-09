"""Service that orchestrates library scans and index synchronisation for the GUI."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ... import app as backend
from ...config import RECENTLY_DELETED_DIR_NAME, WORK_DIR_NAME
from ...errors import IPhotoError
from ..background_task_manager import BackgroundTaskManager
from ..ui.tasks.rescan_worker import RescanSignals, RescanWorker
from ..ui.tasks.scanner_worker import ScannerSignals, ScannerWorker

if TYPE_CHECKING:
    from ...library.manager import LibraryManager
    from ...models.album import Album


class LibraryUpdateService(QObject):
    """Coordinate rescans, Live Photo pairing, and move aftermath bookkeeping."""

    scanProgress = Signal(Path, int, int)
    scanFinished = Signal(Path, bool)
    indexUpdated = Signal(Path)
    linksUpdated = Signal(Path)
    assetReloadRequested = Signal(Path, bool, bool)
    errorRaised = Signal(str)

    def __init__(
        self,
        *,
        task_manager: BackgroundTaskManager,
        current_album_getter: Callable[[], Optional["Album"]],
        library_manager_getter: Callable[[], Optional["LibraryManager"]],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._task_manager = task_manager
        self._current_album_getter = current_album_getter
        self._library_manager_getter = library_manager_getter
        self._scanner_worker: Optional[ScannerWorker] = None
        self._scan_pending = False
        self._stale_album_roots: Dict[str, Path] = {}
        self._album_root_cache: Dict[str, Optional[Path]] = {}

    # ------------------------------------------------------------------
    # Public API used by :class:`~iPhoto.gui.facade.AppFacade`
    # ------------------------------------------------------------------
    def rescan_album(self, album: "Album") -> List[dict]:
        """Synchronously rebuild the album index and emit cache updates."""

        try:
            rows = backend.rescan(album.root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return []

        self.indexUpdated.emit(album.root)
        self.linksUpdated.emit(album.root)
        return rows

    def rescan_album_async(self, album: "Album") -> None:
        """Start an asynchronous rescan for *album* using the background pool."""

        if self._scanner_worker is not None:
            self._scanner_worker.cancel()
            self._scan_pending = True
            return

        filters = album.manifest.get("filters", {}) if isinstance(album.manifest, dict) else {}
        include: Iterable[str] = filters.get("include", backend.DEFAULT_INCLUDE)
        exclude: Iterable[str] = filters.get("exclude", backend.DEFAULT_EXCLUDE)

        signals = ScannerSignals()
        signals.progressUpdated.connect(self._relay_scan_progress)

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
            on_error=lambda root, message: self._on_scan_error(worker, root, message),
            result_payload=lambda root, rows: rows,
        )

    def pair_live(self, album: "Album") -> List[dict]:
        """Rebuild Live Photo pairings for *album* and refresh related views."""

        try:
            groups = backend.pair(album.root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return []

        self.linksUpdated.emit(album.root)
        self.assetReloadRequested.emit(album.root, False, False)
        return [group.__dict__ for group in groups]

    def announce_album_refresh(
        self,
        root: Path,
        *,
        request_reload: bool = True,
        force_reload: bool = False,
        announce_index: bool = False,
    ) -> None:
        """Emit index refresh signals for *root* and optionally request a reload."""

        normalised = Path(root)
        self.indexUpdated.emit(normalised)
        self.linksUpdated.emit(normalised)
        if request_reload:
            self.assetReloadRequested.emit(normalised, announce_index, force_reload)

    def consume_forced_reload(self, root: Path) -> bool:
        """Return ``True`` if *root* was marked for a forced reload."""

        return self._consume_forced_reload(root)

    def reset_cache(self) -> None:
        """Drop cached album resolution results after library re-binding."""

        self._album_root_cache.clear()
        self._stale_album_roots.clear()

    # ------------------------------------------------------------------
    # Slots wired from :class:`AssetMoveService`
    # ------------------------------------------------------------------
    @Slot(Path, Path, list, bool, bool, bool, bool)
    def handle_move_operation_completed(
        self,
        source_root: Path,
        destination_root: Path,
        moved_pairs_raw: list,
        source_ok: bool,
        destination_ok: bool,
        _is_trash_destination: bool,
        is_restore_operation: bool,
    ) -> None:
        """Refresh impacted album views after assets have been moved."""

        moved_pairs: List[Tuple[Path, Path]] = []
        for entry in moved_pairs_raw:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                moved_pairs.append((Path(entry[0]), Path(entry[1])))

        if not moved_pairs:
            return

        library = self._library_manager()
        library_root = library.root() if library is not None else None
        current_album = self._current_album_getter()
        current_root = current_album.root if current_album is not None else None

        refresh_targets: Dict[str, Tuple[Path, bool]] = {}
        blocked_restarts: Set[str] = set()

        def _record_refresh(path: Optional[Path], *, allow_restart: bool = True) -> None:
            if path is None:
                return
            try:
                normalised = self._normalise_path(path)
            except ValueError:
                normalised = path
            key = str(normalised)
            self._mark_album_stale(path)
            if not allow_restart:
                blocked_restarts.add(key)
            should_restart = bool(
                allow_restart
                and key not in blocked_restarts
                and current_root is not None
                and self._paths_equal(current_root, path)
            )
            existing = refresh_targets.get(key)
            if existing is None or (not existing[1] and should_restart):
                refresh_targets[key] = (path, should_restart)

        if source_ok:
            _record_refresh(source_root, allow_restart=False)
        if destination_ok:
            _record_refresh(destination_root)

        additional_roots = self._collect_album_roots_from_pairs(moved_pairs)
        for extra_root in additional_roots:
            _record_refresh(extra_root)

        if library_root is not None:
            touched_library = False
            if source_ok and self._paths_equal(source_root, library_root):
                touched_library = True
            if destination_ok and self._paths_equal(destination_root, library_root):
                touched_library = True
            if not touched_library:
                for original, target in moved_pairs:
                    if self._path_is_descendant(original, library_root) or self._path_is_descendant(
                        target, library_root
                    ):
                        touched_library = True
                        break
            if touched_library:
                _record_refresh(library_root)

        for candidate, should_restart in refresh_targets.values():
            self.indexUpdated.emit(candidate)
            self.linksUpdated.emit(candidate)
            if should_restart:
                target_root = current_root if current_root and self._paths_equal(current_root, candidate) else candidate
                force_reload = self._consume_forced_reload(candidate)
                self.assetReloadRequested.emit(target_root, False, force_reload)

        if not is_restore_operation or not destination_ok:
            return

        if library is None:
            return

        trash_root = library.deleted_directory()
        if trash_root is None:
            return

        if not self._paths_equal(source_root, trash_root):
            return

        library_root_normalised = (
            self._normalise_path(library_root) if library_root is not None else None
        )

        unique_album_roots: Dict[str, Path] = {}
        for _, destination in moved_pairs:
            album_root = Path(destination).parent
            normalised_album = self._normalise_path(album_root)
            if library_root_normalised is not None:
                try:
                    normalised_album.relative_to(library_root_normalised)
                except ValueError:
                    continue
            key = str(normalised_album)
            if key not in unique_album_roots:
                unique_album_roots[key] = album_root

        for album_root in unique_album_roots.values():
            self._refresh_restored_album(album_root, library_root)

    # ------------------------------------------------------------------
    # Internal helpers for scan management
    # ------------------------------------------------------------------
    def _relay_scan_progress(self, root: Path, current: int, total: int) -> None:
        """Forward worker progress updates to keep Qt's type system satisfied."""

        self.scanProgress.emit(root, current, total)

    def _on_scan_finished(
        self,
        worker: ScannerWorker,
        root: Path,
        rows: Sequence[dict],
    ) -> None:
        if self._scanner_worker is not worker:
            return

        if worker.cancelled:
            self.scanFinished.emit(root, True)
            should_restart = self._scan_pending
            self._cleanup_scan_worker()
            if should_restart:
                self._schedule_scan_retry()
            return

        if worker.failed:
            self.scanFinished.emit(root, False)
            should_restart = self._scan_pending
            self._cleanup_scan_worker()
            if should_restart:
                self._schedule_scan_retry()
            return

        materialised_rows = list(rows)

        if root.name == RECENTLY_DELETED_DIR_NAME:
            # When the Recently Deleted album is rescanned we must not lose the
            # restore metadata that was captured at deletion time.  The
            # ``original_rel_path`` value powers quick restores, while the
            # album UUID and subpath pair allows the application to recover from
            # album renames or deletions.  Merge any of those fields back into
            # the freshly scanned rows so the trash index remains authoritative.
            preserved_rows: Dict[str, dict] = {}
            preserved_fields = (
                "original_rel_path",
                "original_album_id",
                "original_album_subpath",
            )
            try:
                for old_row in backend.IndexStore(root).read_all():
                    rel_value = old_row.get("rel")
                    if rel_value is None:
                        continue
                    if not any(field in old_row for field in preserved_fields):
                        continue
                    preserved_rows[str(rel_value)] = old_row
            except IPhotoError:
                # If the old index is unavailable (missing or corrupted) we have
                # nothing to merge, therefore the rescan falls back to fresh
                # metadata produced by the worker.
                preserved_rows = {}

            if preserved_rows:
                for new_row in materialised_rows:
                    rel_value = new_row.get("rel")
                    if rel_value is None:
                        continue
                    cached_row = preserved_rows.get(str(rel_value))
                    if cached_row is None:
                        continue
                    for field in preserved_fields:
                        if field in cached_row:
                            new_row[field] = cached_row[field]

        try:
            # Persist the freshly computed index snapshot immediately so future
            # reloads observe the new metadata rather than the stale cache that
            # existed before the rescan.  The worker keeps the result in memory,
            # therefore we flush both ``index.jsonl`` and ``links.json`` here to
            # mirror the historical facade behaviour before notifying listeners.
            backend._update_index_snapshot(root, materialised_rows)
            backend._ensure_links(root, materialised_rows)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            self.scanFinished.emit(root, False)
        else:
            self.indexUpdated.emit(root)
            self.linksUpdated.emit(root)
            self.scanFinished.emit(root, True)

        should_restart = self._scan_pending
        self._cleanup_scan_worker()

        if should_restart:
            self._schedule_scan_retry()

    def _on_scan_error(
        self,
        worker: ScannerWorker,
        root: Path,
        message: str,
    ) -> None:
        if self._scanner_worker is not worker:
            return

        self.errorRaised.emit(message)
        self.scanFinished.emit(root, False)

        should_restart = self._scan_pending
        self._cleanup_scan_worker()

        if should_restart:
            self._schedule_scan_retry()

    def _cleanup_scan_worker(self) -> None:
        self._scanner_worker = None
        self._scan_pending = False

    def _schedule_scan_retry(self) -> None:
        QTimer.singleShot(0, self._retry_scan_if_album_available)

    def _retry_scan_if_album_available(self) -> None:
        album = self._current_album_getter()
        if album is None:
            return
        self.rescan_album_async(album)

    # ------------------------------------------------------------------
    # Album bookkeeping helpers
    # ------------------------------------------------------------------
    def _current_album_root(self) -> Optional[Path]:
        album = self._current_album_getter()
        return album.root if album is not None else None

    def _library_manager(self) -> Optional["LibraryManager"]:
        return self._library_manager_getter()

    def _mark_album_stale(self, path: Path) -> None:
        try:
            normalised = self._normalise_path(path)
        except ValueError:
            return
        self._stale_album_roots[str(normalised)] = path

    def _consume_forced_reload(self, path: Path) -> bool:
        try:
            normalised = self._normalise_path(path)
        except ValueError:
            return False
        key = str(normalised)
        if key not in self._stale_album_roots:
            return False
        self._stale_album_roots.pop(key, None)
        return True

    def _collect_album_roots_from_pairs(self, pairs: List[Tuple[Path, Path]]) -> Set[Path]:
        if not pairs:
            return set()

        library = self._library_manager()
        if library is None:
            return set()
        library_root = library.root()
        if library_root is None:
            return set()

        library_root_norm = self._normalise_path(library_root)

        affected: Set[Path] = set()
        for original, target in pairs:
            for candidate in (original, target):
                album_root = self._locate_album_root(candidate.parent, library_root_norm)
                if album_root is not None:
                    affected.add(album_root)
        return affected

    def _locate_album_root(self, start: Path, library_root: Path) -> Optional[Path]:
        try:
            candidate = self._normalise_path(start)
        except ValueError:
            candidate = start

        key = str(candidate)
        cached = self._album_root_cache.get(key, ...)
        if cached is not ...:
            return cached

        visited: List[Path] = []
        current = candidate
        while True:
            visited.append(current)
            work_dir = current / WORK_DIR_NAME
            if work_dir.exists():
                album_root = current
                break
            if self._paths_equal(current, library_root) or current.parent == current:
                album_root = None
                break
            current = current.parent

        for entry in visited:
            self._album_root_cache[str(entry)] = album_root

        return album_root

    def _refresh_restored_album(self, album_root: Path, library_root: Optional[Path]) -> None:
        album_root = Path(album_root)
        if not album_root.exists():
            return

        signals = RescanSignals()
        worker = RescanWorker(album_root, signals)
        task_id = self._build_restore_rescan_task_id(album_root)

        def _on_finished(path: Path, succeeded: bool) -> None:
            if not succeeded:
                return

            self.indexUpdated.emit(path)
            self.linksUpdated.emit(path)

            current_album = self._current_album_getter()
            current_root = current_album.root if current_album is not None else None

            if current_root is not None and self._paths_equal(current_root, path):
                force_reload = self._consume_forced_reload(path)
                self.assetReloadRequested.emit(current_root, False, force_reload)
                return

            if (
                library_root is not None
                and current_root is not None
                and self._paths_equal(current_root, library_root)
                and self._path_is_descendant(path, library_root)
            ):
                self.assetReloadRequested.emit(current_root, False, False)

        def _on_error(path: Path, message: str) -> None:
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
        normalised = self._normalise_path(album_root)
        return f"restore-rescan:{normalised}:{uuid.uuid4().hex}"

    def _normalise_path(self, path: Optional[Path]) -> Path:
        if path is None:
            raise ValueError("Cannot normalise a null path.")
        try:
            return path.resolve()
        except OSError:
            return path

    def _paths_equal(self, left: Path, right: Path) -> bool:
        if left == right:
            return True
        return self._normalise_path(left) == self._normalise_path(right)

    def _path_is_descendant(self, candidate: Path, ancestor: Path) -> bool:
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


__all__ = ["LibraryUpdateService"]

