import hashlib
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for thumbnail tests", exc_type=ImportError)
pytest.importorskip(
    "PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError
)
pytest.importorskip("PySide6.QtTest", reason="Qt test utilities unavailable", exc_type=ImportError)

from PySide6.QtCore import QSize
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.config import WORK_DIR_NAME
from iPhotos.src.iPhoto.gui.ui.tasks.thumbnail_loader import ThumbnailLoader

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover - pillow missing or broken
    pytest.skip(
        f"Pillow unavailable for thumbnail loader tests: {exc}",
        allow_module_level=True,
    )


def _create_image(path: Path) -> None:
    image = Image.new("RGB", (16, 16), color="red")
    image.save(path)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_thumbnail_loader_cache_naming(tmp_path: Path, qapp: QApplication) -> None:
    image_path = tmp_path / "IMG_0001.JPG"
    _create_image(image_path)
    loader = ThumbnailLoader()
    loader.reset_for_album(tmp_path)

    spy = QSignalSpy(loader.ready)
    pixmap = loader.request("IMG_0001.JPG", image_path, QSize(192, 192), is_image=True)
    assert pixmap is None
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and spy.count() < 1:
        qapp.processEvents()
        time.sleep(0.05)
    assert spy.count() >= 1

    thumbs_dir = tmp_path / WORK_DIR_NAME / "thumbs"
    files = list(thumbs_dir.iterdir())
    assert len(files) == 1
    filename = files[0].name
    digest = hashlib.sha1("IMG_0001.JPG".encode("utf-8")).hexdigest()
    assert filename.startswith(f"{digest}_")
    assert filename.endswith("_192x192.png")

    # Changing the modification time should produce a new cache entry and
    # remove the stale one.
    os.utime(image_path, None)
    spy = QSignalSpy(loader.ready)
    loader.request("IMG_0001.JPG", image_path, QSize(192, 192), is_image=True)
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and spy.count() < 1:
        qapp.processEvents()
        time.sleep(0.05)
    assert spy.count() >= 1
    files = list(thumbs_dir.iterdir())
    assert len(files) == 1
    assert files[0].name != filename
