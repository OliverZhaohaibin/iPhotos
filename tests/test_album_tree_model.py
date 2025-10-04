import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for tree tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)

from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.gui.ui.models.album_tree_model import AlbumTreeModel, AlbumTreeRole, NodeType
from iPhotos.src.iPhoto.library.manager import LibraryManager


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _create_album(root: Path, title: str, *, child: str | None = None) -> Path:
    album_dir = root / title
    album_dir.mkdir(parents=True, exist_ok=True)
    manifest = album_dir / ".iphoto.album.json"
    manifest.write_text(json.dumps({"schema": "iPhoto/album@1", "title": title}), encoding="utf-8")
    if child is not None:
        child_dir = album_dir / child
        child_dir.mkdir(parents=True, exist_ok=True)
        (child_dir / ".iphoto.album").touch()
        return child_dir
    return album_dir


def _find_child(model: AlbumTreeModel, parent_index, title: str):
    for row in range(model.rowCount(parent_index)):
        index = model.index(row, 0, parent_index)
        if model.data(index) == title:
            return index
    return None


def test_placeholder_when_unbound(qapp: QApplication) -> None:
    manager = LibraryManager()
    model = AlbumTreeModel(manager)
    qapp.processEvents()
    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert model.data(index) == "Bind Basic Library…"
    assert model.data(index, AlbumTreeRole.NODE_TYPE) == NodeType.ACTION


def test_model_populates_albums(tmp_path: Path, qapp: QApplication) -> None:
    root = tmp_path / "Library"
    root.mkdir()
    album_dir = _create_album(root, "Trip", child="Day1")
    manager = LibraryManager()
    manager.bind_path(root)
    qapp.processEvents()
    model = AlbumTreeModel(manager)
    qapp.processEvents()

    header_index = model.index(0, 0)
    assert model.data(header_index) == "📚 Basic Library"
    albums_index = _find_child(model, header_index, "Albums")
    assert albums_index is not None
    trip_index = _find_child(model, albums_index, "Trip")
    assert trip_index is not None
    assert model.data(trip_index, AlbumTreeRole.NODE_TYPE) == NodeType.ALBUM
    child_index = _find_child(model, trip_index, "Day1")
    assert child_index is not None
    assert model.data(child_index, AlbumTreeRole.NODE_TYPE) == NodeType.SUBALBUM

    mapped_index = model.index_for_path(album_dir)
    assert mapped_index.isValid()
    assert model.data(mapped_index) == "Day1"
