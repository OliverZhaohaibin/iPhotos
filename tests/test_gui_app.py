from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover - pillow missing or broken
    pytest.skip(
        f"Pillow unavailable for GUI tests: {exc}",
        allow_module_level=True,
    )

pytest.importorskip("PySide6", reason="PySide6 is required for GUI tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication  # type: ignore  # noqa: E402

from iPhotos.src.iPhoto.gui.facade import AppFacade
from iPhotos.src.iPhoto.gui.ui.models.asset_model import AssetModel, Roles, _ThumbnailJob
from iPhotos.src.iPhoto.config import WORK_DIR_NAME


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
    spy = QSignalSpy(model.dataChanged)
    decoration = model.data(index, Qt.DecorationRole)
    assert isinstance(decoration, QPixmap)
    assert not decoration.isNull()
    spy.wait(500)
    qapp.processEvents()
    refreshed = model.data(index, Qt.DecorationRole)
    assert isinstance(refreshed, QPixmap)
    assert not refreshed.isNull()
    thumbs_dir = tmp_path / WORK_DIR_NAME / "thumbs"
    for _ in range(10):
        qapp.processEvents()
        if thumbs_dir.exists() and any(thumbs_dir.iterdir()):
            break
    assert thumbs_dir.exists()
    assert any(thumbs_dir.iterdir())


def test_thumbnail_job_seek_targets_clamp(tmp_path: Path, qapp: QApplication) -> None:
    dummy_loader = cast(Any, object())
    video_path = tmp_path / "clip.MOV"
    video_path.touch()
    cache_path = tmp_path / "cache.png"
    job = _ThumbnailJob(
        dummy_loader,
        "clip.MOV",
        video_path,
        QSize(192, 192),
        1,
        cache_path,
        is_video=True,
        still_image_time=0.2,
        duration=0.06,
    )
    targets = job._seek_targets()
    assert targets[0] == pytest.approx(0.05)
    assert targets[1:] == [0.0, None]
