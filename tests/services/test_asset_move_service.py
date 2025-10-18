"""Unit tests for :mod:`iPhoto.gui.services.asset_move_service`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import pytest

pytest.importorskip(
    "PySide6",
    reason="PySide6 is required for asset move service tests",
    exc_type=ImportError,
)
pytest.importorskip(
    "PySide6.QtWidgets",
    reason="Qt widgets are required for asset move service tests",
    exc_type=ImportError,
)

from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.gui.services.asset_move_service import AssetMoveService
from iPhotos.src.iPhoto.gui.ui.tasks.move_worker import MoveWorker


@pytest.fixture()
def qapp() -> QApplication:
    """Provide a QApplication instance shared across the module."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _create_service(
    *,
    task_manager,
    asset_list_model,
    current_album,
) -> AssetMoveService:
    """Convenience helper that instantiates the service under test."""

    return AssetMoveService(
        task_manager=task_manager,
        asset_list_model=asset_list_model,
        current_album_getter=current_album,
    )


def test_move_assets_requires_active_album(
    mocker,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    """No album should result in an error and a rollback of optimistic moves."""

    task_manager = mocker.MagicMock()
    asset_list_model = mocker.MagicMock()

    service = _create_service(
        task_manager=task_manager,
        asset_list_model=asset_list_model,
        current_album=lambda: None,
    )

    errors: list[str] = []
    service.errorRaised.connect(errors.append)

    service.move_assets([tmp_path / "file.jpg"], tmp_path / "dest")

    asset_list_model.rollback_pending_moves.assert_called_once()
    assert errors == ["No album is currently open."]
    task_manager.submit_task.assert_not_called()


def test_move_assets_submits_worker_and_emits_completion(
    mocker,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    """Valid requests should produce a background worker and emit results."""

    source_root = tmp_path / "Source"
    destination_root = tmp_path / "Destination"
    source_root.mkdir()
    destination_root.mkdir()
    asset = source_root / "photo.jpg"
    asset.write_bytes(b"data")

    task_manager = mocker.MagicMock()

    class _ListModelSpy:
        """Spy model that records method calls for assertions."""

        def __init__(self) -> None:
            self.pending_rolled_back = 0
            self.finalised: list[list[tuple[Path, Path]]] = []

        def rollback_pending_moves(self) -> None:
            self.pending_rolled_back += 1

        def finalise_move_results(self, pairs: Iterable[tuple[Path, Path]]) -> None:
            self.finalised.append(list(pairs))

        def has_pending_move_placeholders(self) -> bool:
            return False

    list_model = _ListModelSpy()

    album = mocker.MagicMock()
    album.root = source_root

    service = _create_service(
        task_manager=task_manager,
        asset_list_model=list_model,
        current_album=lambda: album,
    )

    results: list[tuple[Path, Path, bool, str]] = []
    service.moveFinished.connect(lambda src, dest, success, message: results.append((src, dest, success, message)))

    service.move_assets([asset], destination_root)

    # The task manager should receive a worker submission with a unique identifier.
    assert task_manager.submit_task.call_count == 1
    kwargs = task_manager.submit_task.call_args.kwargs
    assert kwargs["task_id"].startswith(f"move:{source_root}->{destination_root}:")
    worker = kwargs["worker"]
    assert isinstance(worker, MoveWorker)

    # Simulate the completion callback triggered by the background manager.
    moved_pairs = [(asset, destination_root / asset.name)]
    kwargs["on_finished"](source_root, destination_root, moved_pairs, True, True)

    assert results == [(source_root, destination_root, True, "Moved 1 file.")]
    assert list_model.finalised == [[(asset, destination_root / asset.name)]]
    assert list_model.pending_rolled_back == 0

