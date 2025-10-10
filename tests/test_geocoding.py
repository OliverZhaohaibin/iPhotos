from __future__ import annotations

from pathlib import Path

from iPhotos.src.iPhoto.utils.geocoding import ReverseGeocoder


class DummyLocation:
    def __init__(self, address: dict[str, str]) -> None:
        self.raw = {"address": address}
        self.address = ", ".join(address.values())


class DummyGeolocator:
    def __init__(self, label: dict[str, str]) -> None:
        self._label = label
        self.calls: list[tuple[tuple[float, float], bool, str]] = []

    def reverse(self, coords: tuple[float, float], exactly_one: bool = True, language: str = "en"):
        self.calls.append((coords, exactly_one, language))
        return DummyLocation(self._label)


def test_reverse_geocoder_caches_results(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    geolocator = DummyGeolocator({"city": "London", "suburb": "Theatre District"})
    geocoder = ReverseGeocoder.for_album(album, geolocator=geolocator)

    first = geocoder.lookup(51.5074, -0.1278)
    assert first == "London - Theatre District"
    assert len(geolocator.calls) == 1

    # Second lookup should hit the cache rather than the geolocator.
    second = geocoder.lookup(51.5074, -0.1278)
    assert second == "London - Theatre District"
    assert len(geolocator.calls) == 1

    cache_file = album / ".iPhoto" / "geocache.json"
    assert cache_file.exists()


def test_reverse_geocoder_handles_failures(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()

    class FailingGeolocator:
        def reverse(self, *args, **kwargs):  # noqa: ANN002, ANN003 - signature mirrors geopy
            raise RuntimeError("network unavailable")

    geocoder = ReverseGeocoder.for_album(album, geolocator=FailingGeolocator())
    result = geocoder.lookup(10.0, 20.0)
    assert result is None
