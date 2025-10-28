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

from iPhotos.src.iPhoto.cache.index_store import IndexStore
from iPhotos.src.iPhoto.gui.services.asset_move_service import AssetMoveService
from iPhotos.src.iPhoto.gui.ui.tasks import move_worker as move_worker_module
from iPhotos.src.iPhoto.gui.ui.tasks.move_worker import MoveSignals, MoveWorker
from iPhotos.src.iPhoto.library.manager import LibraryManager


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
    library_manager=None,
) -> AssetMoveService:
    """Convenience helper that instantiates the service under test."""

    return AssetMoveService(
        task_manager=task_manager,
        asset_list_model=asset_list_model,
        current_album_getter=current_album,
        library_manager_getter=(lambda: library_manager),
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
    detailed_results: list[tuple] = []

    service.moveFinished.connect(
        lambda src, dest, success, message: results.append((src, dest, success, message))
    )
    service.moveCompletedDetailed.connect(detailed_results.append)

    service.move_assets([asset], destination_root)

    # The task manager should receive a worker submission with a unique identifier.
    assert task_manager.submit_task.call_count == 1
    kwargs = task_manager.submit_task.call_args.kwargs
    assert kwargs["task_id"].startswith(
        f"move:move:{source_root}->{destination_root}:"
    )
    worker = kwargs["worker"]
    assert isinstance(worker, MoveWorker)

    # Simulate the completion callback triggered by the background manager.
    moved_pairs = [(asset, destination_root / asset.name)]
    kwargs["on_finished"](source_root, destination_root, moved_pairs, True, True)

    assert results == [(source_root, destination_root, True, "Moved 1 item.")]
    assert list_model.finalised == [[(asset, destination_root / asset.name)]]
    assert list_model.pending_rolled_back == 0
    assert detailed_results == [
        (
            source_root,
            destination_root,
            [(asset, destination_root / asset.name)],
            True,
            True,
            False,
            False,
        )
    ]


def test_restore_repopulates_library_index(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring from trash should reinsert rows into the library-wide index."""

    library_root = tmp_path / "Library"
    album_root = library_root / "AlbumA"

    library_root.mkdir()
    album_root.mkdir(parents=True)

    library_manager = LibraryManager()
    library_manager.bind_path(library_root)
    resolved_trash = library_manager.ensure_deleted_directory()
    assert resolved_trash is not None
    trash_root = resolved_trash

    asset_name = "IMG_0001.JPG"
    trashed_asset = trash_root / asset_name
    trashed_asset.write_bytes(b"stub")

    def _fake_process_media_paths(root: Path, image_paths, video_paths):
        """Return minimal index rows keyed by their relative path."""

        rows = []
        for candidate in list(image_paths) + list(video_paths):
            rows.append({"rel": candidate.resolve().relative_to(root).as_posix()})
        return rows

    monkeypatch.setattr(move_worker_module, "process_media_paths", _fake_process_media_paths)
    monkeypatch.setattr(move_worker_module.backend, "pair", lambda _root: None)

    restore_signals = MoveSignals()
    worker = MoveWorker(
        [trashed_asset],
        trash_root,
        album_root,
        restore_signals,
        library_root=library_root,
        trash_root=trash_root,
        is_restore=True,
    )

    worker.run()

    restored_asset = album_root / asset_name
    assert restored_asset.exists()
    assert not trashed_asset.exists()

    library_rows = list(IndexStore(library_root).read_all())
    assert any(row.get("rel") == f"AlbumA/{asset_name}" for row in library_rows)

    album_rows = list(IndexStore(album_root).read_all())
    assert any(row.get("rel") == asset_name for row in album_rows)


def test_move_from_library_root_updates_source_album_index(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving from the library view must trim the concrete source album index."""

    library_root = tmp_path / "Library"
    album_a = library_root / "AlbumA"
    album_b = library_root / "AlbumB"
    library_root.mkdir()
    album_a.mkdir(parents=True)
    album_b.mkdir(parents=True)

    asset = album_a / "IMG_0100.JPG"
    asset.write_bytes(b"asset")

    def _fake_process_media_paths(root: Path, image_paths, video_paths):
        rows = []
        for candidate in list(image_paths) + list(video_paths):
            rel = candidate.resolve().relative_to(root).as_posix()
            rows.append({"rel": rel})
        return rows

    monkeypatch.setattr(move_worker_module, "process_media_paths", _fake_process_media_paths)
    monkeypatch.setattr(move_worker_module.backend, "pair", lambda _root: None)

    IndexStore(library_root).write_rows(
        [{"rel": f"AlbumA/{asset.name}", "abs": str(asset.resolve())}]
    )
    IndexStore(album_a).write_rows([{"rel": asset.name, "abs": str(asset.resolve())}])

    signals = MoveSignals()
    worker = MoveWorker(
        [asset],
        library_root,
        album_b,
        signals,
        library_root=library_root,
    )

    worker.run()

    album_a_rows = list(IndexStore(album_a).read_all())
    assert album_a_rows == []

    library_rows = list(IndexStore(library_root).read_all())
    assert library_rows == [{"rel": f"AlbumB/{asset.name}"}]

    album_b_rows = list(IndexStore(album_b).read_all())
    assert album_b_rows == [{"rel": asset.name}]

