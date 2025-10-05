from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from iPhotos.src.iPhoto.io.metadata import read_image_meta


def _make_exif_image(path: Path, dt: str, offset: str | None = None) -> None:
    image_module = pytest.importorskip(
        "PIL.Image", reason="Pillow is required to generate test images"
    )

    exif_factory = getattr(image_module, "Exif", None)
    if exif_factory is None:
        pytest.skip("Pillow build does not support Exif writing")

    exif = exif_factory()
    exif[36867] = dt  # DateTimeOriginal
    if offset is not None:
        exif[36880] = offset  # OffsetTimeOriginal

    image = image_module.new("RGB", (8, 8), color="white")
    image.save(path, format="JPEG", exif=exif)


def test_read_image_meta_uses_offset_when_available(tmp_path: Path) -> None:
    photo = tmp_path / "offset.jpg"
    _make_exif_image(photo, "2024:01:01 12:00:00", "+02:00")

    info = read_image_meta(photo)
    assert info["dt"] == "2024-01-01T10:00:00Z"


def test_read_image_meta_falls_back_to_local_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    photo = tmp_path / "local.jpg"
    _make_exif_image(photo, "2024:06:10 09:30:00")

    class _FakeDt(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return super().now(timezone.utc if tz is None else tz)

    monkeypatch.setattr("iPhotos.src.iPhoto.io.metadata.datetime", _FakeDt)

    info = read_image_meta(photo)
    assert info["dt"] == "2024-06-10T09:30:00Z"
