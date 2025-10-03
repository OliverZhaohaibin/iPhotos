from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("PySide6", reason="PySide6 is required for GUI tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication  # type: ignore  # noqa: E402

from iPhoto.gui.facade import AppFacade
from iPhoto.gui.ui.models.asset_model import AssetModel, Roles


def _create_image(path: Path) -> None:
    image = Image.new("RGB", (8, 8), color="blue")
    image.save(path)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_facade_open_album_emits_signals(tmp_path: Path, qapp: QApplication) -> None:
    asset = tmp_path / "IMG_1001.JPG"
    _create_image(asset)
    facade = AppFacade()
    received: list[str] = []
    facade.albumOpened.connect(lambda _: received.append("opened"))
    facade.indexUpdated.connect(lambda _: received.append("index"))
    facade.linksUpdated.connect(lambda _: received.append("links"))
    album = facade.open_album(tmp_path)
    qapp.processEvents()
    assert album is not None
    assert (tmp_path / ".iPhoto" / "index.jsonl").exists()
    assert "opened" in received and "index" in received


def test_asset_model_populates_rows(tmp_path: Path, qapp: QApplication) -> None:
    asset = tmp_path / "IMG_2001.JPG"
    _create_image(asset)
    facade = AppFacade()
    model = AssetModel(facade)
    facade.open_album(tmp_path)
    qapp.processEvents()
    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert model.data(index, Roles.REL) == "IMG_2001.JPG"
    assert model.data(index, Roles.FEATURED) is False
    decoration = model.data(index, Qt.DecorationRole)
    assert isinstance(decoration, QPixmap)
    assert not decoration.isNull()
