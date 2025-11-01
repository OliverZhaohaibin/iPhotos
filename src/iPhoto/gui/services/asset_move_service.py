"""Service dedicated to moving assets between albums on behalf of the facade."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from ..background_task_manager import BackgroundTaskManager
from ..ui.tasks.move_worker import MoveSignals, MoveWorker

if TYPE_CHECKING:
    from ...library.manager import LibraryManager
    from ...models.album import Album
    from ..ui.models.asset_list_model import AssetListModel


class AssetMoveService(QObject):
    """Validate and execute asset move operations, surfacing progress events."""

    moveStarted = Signal(Path, Path)
    moveProgress = Signal(Path, int, int)
    moveFinished = Signal(Path, Path, bool, str)
    # ``moveCompletedDetailed`` mirrors the worker payload so higher-level
    # components (such as :class:`AppFacade`) can react to restore operations
    # with additional bookkeeping, e.g. refreshing album views.  Qt's signal
    # type system does not understand ``list[tuple[Path, Path]]`` so we emit
    # the raw ``list`` that contains :class:`pathlib.Path` pairs alongside the
    # worker flags.
    moveCompletedDetailed = Signal(Path, Path, list, bool, bool, bool, bool)
    errorRaised = Signal(str)

    def __init__(
        self,
        *,
        task_manager: BackgroundTaskManager,
        asset_list_model: "AssetListModel",
        current_album_getter: Callable[[], Optional["Album"]],
        library_manager_getter: Callable[[], Optional["LibraryManager"]],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._task_manager = task_manager
        self._asset_list_model = asset_list_model
        self._current_album_getter = current_album_getter
        self._library_manager_getter = library_manager_getter

    def move_assets(
        self,
        sources: Iterable[Path],
        destination: Path,
        *,
        operation: str = "move",
    ) -> None:
        """Validate *sources* and queue a worker for the requested *operation*.

        ``operation`` accepts ``"move"`` (default), ``"delete"`` when moving items
        into Recently Deleted, and ``"restore"`` when returning files from the trash
        to their original albums.  The distinction matters because restore jobs must
        avoid annotating the destination index with ``original_rel_path`` entries
        while delete jobs must do the opposite.
        """

        operation_normalized = operation.lower()
        if operation_normalized not in {"move", "delete", "restore"}:
            self.errorRaised.emit(f"Unsupported move operation: {operation}")
            self._asset_list_model.rollback_pending_moves()
            return

        album = self._current_album_getter()
        if album is None:
            self._asset_list_model.rollback_pending_moves()
            self.errorRaised.emit("No album is currently open.")
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

        normalized: List[Path] = []
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

        rel_paths: List[str] = []
        for resolved in normalized:
            try:
                rel_paths.append(resolved.relative_to(source_root).as_posix())
            except ValueError:
                continue

        signals = MoveSignals()
        signals.started.connect(self._on_move_started)
        signals.progress.connect(self._on_move_progress)

        library_root: Optional[Path] = None
        trash_root: Optional[Path] = None
        library_manager = self._library_manager_getter()
        if library_manager is not None:
            library_root = library_manager.root()
            trash_candidate = library_manager.deleted_directory()
            if trash_candidate is not None:
                try:
                    trash_resolved = trash_candidate.resolve()
                except OSError:
                    trash_resolved = trash_candidate
                try:
                    destination_resolved = destination_root.resolve()
                except OSError:
                    destination_resolved = destination_root
                if destination_resolved == trash_resolved:
                    trash_root = trash_candidate

        is_delete_operation = operation_normalized == "delete"
        is_restore_operation = operation_normalized == "restore"

        if is_delete_operation and library_root is None:
            self._asset_list_model.rollback_pending_moves()
            self.errorRaised.emit(
                "Basic Library root is unavailable; cannot delete items safely."
            )
            return

        if is_delete_operation and trash_root is None:
            self._asset_list_model.rollback_pending_moves()
            self.errorRaised.emit("Recently Deleted folder is unavailable.")
            return

        if is_restore_operation:
            if trash_root is None:
                trash_root = library_manager.deleted_directory() if library_manager else None
            if trash_root is None:
                self._asset_list_model.rollback_pending_moves()
                self.errorRaised.emit("Recently Deleted folder is unavailable.")
                return
            try:
                source_resolved = source_root.resolve()
            except OSError:
                source_resolved = source_root
            try:
                trash_resolved = trash_root.resolve()
            except OSError:
                trash_resolved = trash_root
            if source_resolved != trash_resolved:
                self._asset_list_model.rollback_pending_moves()
                self.errorRaised.emit(
                    "Restore operations must be triggered from Recently Deleted."
                )
                return
            try:
                destination_resolved = destination_root.resolve()
            except OSError:
                destination_resolved = destination_root
            if destination_resolved == trash_resolved:
                self._asset_list_model.rollback_pending_moves()
                self.errorRaised.emit("Cannot restore items back into Recently Deleted.")
                return

        is_source_main_view = False
        if library_root is not None:
            try:
                is_source_main_view = library_root.resolve() == source_root.resolve()
            except OSError:
                is_source_main_view = library_root == source_root

        if operation_normalized == "move" and rel_paths:
            self._asset_list_model.update_rows_for_move(
                rel_paths,
                destination_root,
                is_source_main_view=is_source_main_view,
            )

        worker = MoveWorker(
            normalized,
            source_root,
            destination_root,
            signals,
            library_root=library_root,
            trash_root=trash_root,
            is_restore=is_restore_operation,
        )
        unique_task_id = (
            f"move:{operation_normalized}:{source_root}->{destination_root}:{uuid.uuid4().hex}"
        )
        # Move requests share their origin and target directories, so we need a unique
        # suffix on the identifier to allow queuing multiple operations without the
        # BackgroundTaskManager rejecting the submission as a duplicate.
        self._task_manager.submit_task(
            task_id=unique_task_id,
            worker=worker,
            started=signals.started,
            progress=signals.progress,
            finished=signals.finished,
            error=signals.error,
            pause_watcher=True,
            on_finished=lambda src, dest, moved, source_ok, destination_ok, *, move_worker=worker: self._handle_move_finished(
                src,
                dest,
                moved,
                source_ok,
                destination_ok,
                move_worker,
            ),
            on_error=self._handle_worker_error,
            result_payload=lambda src, dest, moved, *_: moved,
        )

    def _handle_move_finished(
        self,
        source_root: Path,
        destination_root: Path,
        moved: Sequence[Sequence[Path]],
        source_ok: bool,
        destination_ok: bool,
        worker: MoveWorker,
    ) -> None:
        """Mirror worker completion back into the optimistic UI state."""

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

        # Surface the rich completion payload so listeners can distinguish
        # between deletes, restores, and plain moves without replicating the
        # worker bookkeeping logic in multiple layers.
        self.moveCompletedDetailed.emit(
            source_root,
            destination_root,
            moved_pairs,
            source_ok,
            destination_ok,
            worker.is_trash_destination,
            worker.is_restore_operation,
        )

        if moved_pairs:
            self._asset_list_model.finalise_move_results(moved_pairs)
        if self._asset_list_model.has_pending_move_placeholders():
            self._asset_list_model.rollback_pending_moves()

        delete_operation = worker.is_trash_destination and not worker.is_restore_operation
        restore_operation = worker.is_restore_operation
        if not moved_pairs:
            if delete_operation:
                message = "No items were deleted."
            elif restore_operation:
                # Returning an empty string prevents the status bar from
                # showing the standard restore completion toast when the user
                # declined the fallback or when no files could be restored.
                # The controller clears the transient text in this scenario so
                # the previous progress copy does not linger.
                message = ""
            else:
                message = "No files were moved."
        else:
            label = "item" if len(moved_pairs) == 1 else "items"
            if restore_operation:
                verb = "Restored"
                if source_ok and destination_ok:
                    message = f"{verb} {len(moved_pairs)} {label}."
                elif source_ok and not destination_ok:
                    message = (
                        f"{verb} {len(moved_pairs)} {label}, but updating the destination album failed."
                    )
                elif destination_ok and not source_ok:
                    message = (
                        f"{verb} {len(moved_pairs)} {label}, but updating Recently Deleted failed."
                    )
                else:
                    message = (
                        f"{verb} {len(moved_pairs)} {label}, but updating Recently Deleted "
                        "and the destination album failed."
                    )
            else:
                verb = "Deleted" if delete_operation else "Moved"
                if source_ok and destination_ok:
                    message = f"{verb} {len(moved_pairs)} {label}."
                elif delete_operation:
                    if source_ok and not destination_ok:
                        message = (
                            f"{verb} {len(moved_pairs)} {label}, but updating Recently Deleted failed."
                        )
                    elif destination_ok and not source_ok:
                        message = (
                            f"{verb} {len(moved_pairs)} {label}, but updating the original album failed."
                        )
                    else:
                        message = (
                            f"{verb} {len(moved_pairs)} {label}, but updating the original album "
                            "and Recently Deleted failed."
                        )
                elif source_ok or destination_ok:
                    message = (
                        f"{verb} {len(moved_pairs)} {label}, but refreshing one album failed."
                    )
                else:
                    message = (
                        f"{verb} {len(moved_pairs)} {label}, but refreshing both albums failed."
                    )

        self.moveFinished.emit(source_root, destination_root, success, message)

    @Slot(Path, Path)
    def _on_move_started(self, source: Path, destination: Path) -> None:
        """Emit :attr:`moveStarted` while complying with Nuitka's slot validation."""

        self.moveStarted.emit(source, destination)

    @Slot(Path, int, int)
    def _on_move_progress(self, root: Path, current: int, total: int) -> None:
        """Emit :attr:`moveProgress` for worker updates via a dedicated slot."""

        self.moveProgress.emit(root, current, total)

    @Slot(str)
    def _handle_worker_error(self, message: str) -> None:
        """Relay worker errors while keeping Nuitka satisfied with the slot type.

        Nuitka validates the callable passed to :func:`Signal.connect` eagerly and
        refuses method descriptors such as :py:meth:`Signal.emit`.  Routing the
        signal through a dedicated slot preserves the original behaviour—
        forwarding the text message through :attr:`errorRaised`—without
        triggering ``SystemError`` during compilation.
        """

        self.errorRaised.emit(message)


__all__ = ["AssetMoveService"]
