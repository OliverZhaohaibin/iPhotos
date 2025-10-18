"""Service dedicated to moving assets between albums on behalf of the facade."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from ..background_task_manager import BackgroundTaskManager
from ..ui.tasks.move_worker import MoveSignals, MoveWorker

if TYPE_CHECKING:
    from ...models.album import Album
    from ..ui.models.asset_list_model import AssetListModel


class AssetMoveService(QObject):
    """Validate and execute asset move operations, surfacing progress events."""

    moveStarted = Signal(Path, Path)
    moveProgress = Signal(Path, int, int)
    moveFinished = Signal(Path, Path, bool, str)
    errorRaised = Signal(str)

    def __init__(
        self,
        *,
        task_manager: BackgroundTaskManager,
        asset_list_model: "AssetListModel",
        current_album_getter: Callable[[], Optional["Album"]],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._task_manager = task_manager
        self._asset_list_model = asset_list_model
        self._current_album_getter = current_album_getter

    def move_assets(self, sources: Iterable[Path], destination: Path) -> None:
        """Validate *sources* and schedule a worker to move them into *destination*."""

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

        signals = MoveSignals()
        signals.started.connect(self.moveStarted.emit)
        signals.progress.connect(self.moveProgress.emit)

        worker = MoveWorker(normalized, source_root, destination_root, signals)
        unique_task_id = f"move:{source_root}->{destination_root}:{uuid.uuid4().hex}"
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
            on_error=self.errorRaised.emit,
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

        if moved_pairs:
            self._asset_list_model.finalise_move_results(moved_pairs)
        if self._asset_list_model.has_pending_move_placeholders():
            self._asset_list_model.rollback_pending_moves()

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


__all__ = ["AssetMoveService"]
