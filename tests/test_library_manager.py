from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for library tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)
pytest.importorskip("PySide6.QtTest", reason="Qt test helpers not available", exc_type=ImportError)

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.errors import AlbumDepthError, LibraryUnavailableError
from iPhotos.src.iPhoto.library.manager import LibraryManager


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _write_manifest(path: Path, title: str) -> None:
    payload = {
        "schema": "iPhoto/album@1",
        "title": title,
        "filters": {},
    }
    manifest = path / ".iphoto.album.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")


def test_bind_and_scan_tree(tmp_path: Path, qapp: QApplication) -> None:
    root = tmp_path / "Library"
    manager = LibraryManager()
    spy = QSignalSpy(manager.treeUpdated)
    with pytest.raises(LibraryUnavailableError):
        manager.bind_path(root)
    album = root / "Trip"
    child = album / "Day1"
    child.mkdir(parents=True)
    _write_manifest(album, "Summer Trip")
    manager.bind_path(root)
    qapp.processEvents()
    assert spy.count() >= 1
    albums = manager.list_albums()
    assert len(albums) == 1
    assert albums[0].title == "Summer Trip"
    children = manager.list_children(albums[0])
    assert len(children) == 1
    assert children[0].level == 2
    assert children[0].title == "Day1"


def test_create_and_rename_album(tmp_path: Path, qapp: QApplication) -> None:
    root = tmp_path / "Library"
    root.mkdir()
    manager = LibraryManager()
    manager.bind_path(root)
    created = manager.create_album("Paris")
    assert created.level == 1
    assert (created.path / ".iphoto.album.json").exists()
    sub = manager.create_subalbum(created, "Day0")
    assert sub.level == 2
    with pytest.raises(AlbumDepthError):
        manager.create_subalbum(sub, "TooDeep")
    manager.rename_album(sub, "Arrival")
    qapp.processEvents()
    refreshed_parent = next(
        node for node in manager.list_albums() if node.path == created.path
    )
    refreshed_children = manager.list_children(refreshed_parent)
    assert any(child.title == "Arrival" for child in refreshed_children)
    manifest_path = next(
        child.path / ".iphoto.album.json"
        for child in refreshed_children
        if child.title == "Arrival"
    )
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["title"] == "Arrival"


def test_ensure_manifest_generates_defaults(tmp_path: Path) -> None:
    root = tmp_path / "Library"
    album_dir = root / "NoManifest"
    album_dir.mkdir(parents=True)
    manager = LibraryManager()
    manager.bind_path(root)
    node = next(node for node in manager.list_albums() if node.path == album_dir)
    manifest_path = manager.ensure_manifest(node)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["title"] == "NoManifest"
    assert data["schema"] == "iPhoto/album@1"
