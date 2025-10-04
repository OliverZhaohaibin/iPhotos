import base64
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for sidebar tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)

from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.cache.index_store import IndexStore
from iPhotos.src.iPhoto.gui.facade import AppFacade
from iPhotos.src.iPhoto.gui.ui.widgets.album_sidebar import AlbumSidebar
from iPhotos.src.iPhoto.library.manager import LibraryManager


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _write_manifest(path: Path, title: str) -> None:
    payload = {"schema": "iPhoto/album@1", "title": title, "filters": {}}
    (path / ".iphoto.album.json").write_text(json.dumps(payload), encoding="utf-8")


_PNG_DATA = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y4nKwAAAABJRU5ErkJggg=="
)


def test_all_photos_selection_emits_signal(tmp_path: Path, qapp: QApplication) -> None:
    root = tmp_path / "Library"
    album_dir = root / "Trip"
    album_dir.mkdir(parents=True)
    _write_manifest(album_dir, "Trip")
    manager = LibraryManager()
    manager.bind_path(root)
    qapp.processEvents()

    sidebar = AlbumSidebar(manager)
    qapp.processEvents()

    triggered: list[bool] = []
    sidebar.allPhotosSelected.connect(lambda: triggered.append(True))
    sidebar.select_all_photos()
    qapp.processEvents()

    assert triggered, "Selecting All Photos should emit allPhotosSelected"


def test_videos_selection_emits_static_signal(tmp_path: Path, qapp: QApplication) -> None:
    root = tmp_path / "Library"
    album_dir = root / "Trip"
    album_dir.mkdir(parents=True)
    _write_manifest(album_dir, "Trip")
    manager = LibraryManager()
    manager.bind_path(root)
    qapp.processEvents()

    sidebar = AlbumSidebar(manager)
    qapp.processEvents()

    triggered: list[str] = []
    sidebar.staticNodeSelected.connect(lambda title: triggered.append(title))
    sidebar.select_static_node("Videos")
    qapp.processEvents()

    assert triggered == ["Videos"], "Videos selection should emit staticNodeSelected"


def test_opening_library_root_indexes_nested_assets(tmp_path: Path, qapp: QApplication) -> None:
    root = tmp_path / "Library"
    album_dir = root / "Trip"
    child_dir = album_dir / "Day1"
    child_dir.mkdir(parents=True)
    _write_manifest(album_dir, "Trip")
    (child_dir / ".iphoto.album").touch()
    (album_dir / "photo.PNG").write_bytes(_PNG_DATA)
    (child_dir / "nested.PNG").write_bytes(_PNG_DATA)

    facade = AppFacade()
    album = facade.open_album(root)
    assert album is not None

    rows = list(IndexStore(root).read_all())
    rel_paths = {row["rel"] for row in rows}
    assert rel_paths == {"Trip/photo.PNG", "Trip/Day1/nested.PNG"}
