from __future__ import annotations

from datetime import timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from iPhotos.src.iPhoto.io.metadata import read_image_meta


def _to_dms(value: float) -> tuple[Fraction, Fraction, Fraction]:
    absolute = abs(value)
    degrees = int(absolute)
    minutes_float = (absolute - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    return (
        Fraction(degrees, 1),
        Fraction(minutes, 1),
        Fraction(seconds).limit_denominator(1_000_000),
    )


def _make_exif_image(
    path: Path,
    dt: str,
    offset: str | None = None,
    gps: tuple[float, float] | None = None,
) -> None:
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
    if gps is not None:
        lat, lon = gps
        exif[34853] = {
            1: "N" if lat >= 0 else "S",
            2: _to_dms(lat),
            3: "E" if lon >= 0 else "W",
            4: _to_dms(lon),
        }

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

    fake_tz = timezone(timedelta(hours=2))
    monkeypatch.setattr("iPhotos.src.iPhoto.io.metadata.gettz", lambda: fake_tz)

    info = read_image_meta(photo)
    assert info["dt"] == "2024-06-10T07:30:00Z"


def test_read_image_meta_extracts_gps_coordinates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class DummyImage:
        width = 8
        height = 8
        format = "JPEG"

        def __enter__(self) -> "DummyImage":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def getexif(self) -> dict[int, object]:
            return {
                34853: {
                    1: "N",
                    2: ((51, 1), (30, 1), (2808, 100)),
                    3: "W",
                    4: ((0, 1), (7, 1), (4008, 100)),
                }
            }

    monkeypatch.setattr(
        "iPhotos.src.iPhoto.io.metadata.Image.open", lambda path: DummyImage()
    )

    info = read_image_meta(tmp_path / "dummy.jpg")
    assert info["gps"] == pytest.approx({"lat": 51.5078, "lon": -0.1278}, rel=1e-4)


def test_read_image_meta_accepts_string_keyed_gps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class DummyImage:
        width = 8
        height = 8
        format = "JPEG"

        def __enter__(self) -> "DummyImage":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def getexif(self) -> dict[int, object]:
            return {
                34853: {
                    "GPSLatitudeRef": b"N",
                    "GPSLatitude": ((34, 1), (3, 1), (3600, 100)),
                    "GPSLongitudeRef": "E",
                    "GPSLongitude": ((118, 1), (15, 1), (0, 1)),
                }
            }

    monkeypatch.setattr(
        "iPhotos.src.iPhoto.io.metadata.Image.open", lambda path: DummyImage()
    )

    info = read_image_meta(tmp_path / "dummy-string-keys.jpg")
    assert info["gps"] == pytest.approx({"lat": 34.06, "lon": 118.25}, rel=1e-4)
