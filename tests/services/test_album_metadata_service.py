"""Unit tests for :mod:`iPhoto.gui.services.album_metadata_service`."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import os
import pytest

pytest.importorskip(
    "PySide6",
    reason="PySide6 is required for album metadata service tests",
    exc_type=ImportError,
)
pytest.importorskip(
    "PySide6.QtWidgets",
    reason="Qt widgets are required for album metadata service tests",
    exc_type=ImportError,
)

from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.errors import IPhotoError
from iPhotos.src.iPhoto.gui.services.album_metadata_service import AlbumMetadataService


@pytest.fixture()
def qapp() -> QApplication:
    """Ensure a QApplication instance exists for QObject-based services."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class DummyAlbum:
    """Simple stand-in for :class:`Album` capturing manifest mutations."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest: dict[str, list[str]] = {"featured": []}
        self.cover: Optional[str] = None
        self.saved = 0

    def set_cover(self, rel: str) -> None:
        """Record the last requested cover path."""

        self.cover = rel

    def add_featured(self, ref: str) -> None:
        """Append a featured entry if it is not already present."""

        if ref not in self.manifest["featured"]:
            self.manifest["featured"].append(ref)

    def remove_featured(self, ref: str) -> None:
        """Remove a featured entry when it exists."""

        if ref in self.manifest["featured"]:
            self.manifest["featured"].remove(ref)

    def save(self) -> None:
        """Simulate a successful manifest write."""

        self.saved += 1


def _build_service(
    *,
    asset_model,
    current_album: Callable[[], Optional[DummyAlbum]],
    library_manager_getter: Callable[[], Optional[object]],
    refresh,
) -> AlbumMetadataService:
    """Create a service instance with timer scheduling patched for tests."""

    service = AlbumMetadataService(
        asset_list_model=asset_model,
        current_album_getter=current_album,
        library_manager_getter=library_manager_getter,
        refresh_view=refresh,
    )
    return service


def test_toggle_featured_updates_current_and_library_album(
    monkeypatch: pytest.MonkeyPatch,
    mocker,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    """Toggling an asset should update both the active album and the library root."""

    album_root = tmp_path / "Albums" / "Trip"
    library_root = tmp_path / "Albums"
    album_root.mkdir(parents=True)
    dummy_album = DummyAlbum(album_root)
    root_album = DummyAlbum(library_root)

    # Patch ``Album.open`` to return the dummy root album when invoked.
    monkeypatch.setattr(
        "iPhotos.src.iPhoto.gui.services.album_metadata_service.Album.open",
        lambda path: root_album if Path(path) == library_root else dummy_album,
    )
    # Avoid waiting for Qt timers in tests by executing callbacks immediately.
    monkeypatch.setattr(
        "iPhotos.src.iPhoto.gui.services.album_metadata_service.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    manager = mocker.MagicMock()
    manager.root.return_value = library_root
    asset_model = mocker.MagicMock()
    refresh = mocker.MagicMock()

    service = _build_service(
        asset_model=asset_model,
        current_album=lambda: dummy_album,
        library_manager_getter=lambda: manager,
        refresh=refresh,
    )

    result = service.toggle_featured(dummy_album, "photo.jpg")

    # The current album must mark the asset as featured and persist the change.
    assert result is True
    assert dummy_album.manifest["featured"] == ["photo.jpg"]
    assert dummy_album.saved == 1
    asset_model.update_featured_status.assert_called_once_with("photo.jpg", True)

    # The library root receives the fully qualified path for shared favorites.
    assert root_album.manifest["featured"] == ["Trip/photo.jpg"]
    assert root_album.saved == 1
    refresh.assert_not_called()


def test_toggle_featured_rolls_back_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    mocker,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    """Errors when saving should keep the manifest and model unchanged."""

    album_root = tmp_path / "Album"
    album_root.mkdir()
    dummy_album = DummyAlbum(album_root)
    dummy_album.manifest["featured"] = ["photo.jpg"]

    def failing_save() -> None:
        raise IPhotoError("boom")

    dummy_album.save = failing_save  # type: ignore[assignment]

    monkeypatch.setattr(
        "iPhotos.src.iPhoto.gui.services.album_metadata_service.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    asset_model = mocker.MagicMock()
    refresh = mocker.MagicMock()
    errors: list[str] = []

    service = _build_service(
        asset_model=asset_model,
        current_album=lambda: dummy_album,
        library_manager_getter=lambda: None,
        refresh=refresh,
    )
    service.errorRaised.connect(errors.append)

    result = service.toggle_featured(dummy_album, "photo.jpg")

    # The method should report the original state and avoid touching the model.
    assert result is True
    assert dummy_album.manifest["featured"] == ["photo.jpg"]
    asset_model.update_featured_status.assert_not_called()
    assert errors == ["boom"]
    refresh.assert_not_called()


def test_ensure_featured_entries_updates_album(
    monkeypatch: pytest.MonkeyPatch,
    mocker,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    """Recently imported files should be promoted to featured assets when requested."""

    library_root = tmp_path / "Library"
    album_root = library_root / "Hike"
    album_root.mkdir(parents=True)
    imported = [album_root / "a.jpg", album_root / "b.jpg", library_root / "skip.txt"]

    current_album: Optional[DummyAlbum] = None
    dummy_album = DummyAlbum(album_root)

    monkeypatch.setattr(
        "iPhotos.src.iPhoto.gui.services.album_metadata_service.Album.open",
        lambda path: dummy_album if Path(path) == album_root else None,
    )
    monkeypatch.setattr(
        "iPhotos.src.iPhoto.gui.services.album_metadata_service.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    asset_model = mocker.MagicMock()
    refresh = mocker.MagicMock()

    service = _build_service(
        asset_model=asset_model,
        current_album=lambda: current_album,
        library_manager_getter=lambda: None,
        refresh=refresh,
    )

    service.ensure_featured_entries(album_root, imported)

    # Only paths located inside the album should be recorded.
    assert sorted(dummy_album.manifest["featured"]) == ["a.jpg", "b.jpg"]
    assert dummy_album.saved == 1
    refresh.assert_not_called()
    asset_model.update_featured_status.assert_not_called()
