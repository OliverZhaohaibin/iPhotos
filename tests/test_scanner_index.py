from __future__ import annotations

from pathlib import Path

from PIL import Image

from iPhoto.config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE
from iPhoto.io.scanner import scan_album


def create_image(path: Path) -> None:
    img = Image.new("RGB", (10, 10), color="red")
    img.save(path)


def test_scan_album_produces_rows(tmp_path: Path) -> None:
    asset = tmp_path / "IMG_0001.JPG"
    create_image(asset)
    rows = list(scan_album(tmp_path, DEFAULT_INCLUDE, DEFAULT_EXCLUDE))
    assert len(rows) == 1
    row = rows[0]
    assert row["rel"] == "IMG_0001.JPG"
    assert row["w"] == 10 and row["h"] == 10
