from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import iPhoto.utils.geocoding as geocoding
from iPhoto.utils.geocoding import ReverseGeocoder


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with a clean geocoder module state."""

    # Reset the optional dependency hook so tests can inject their own stub.
    monkeypatch.setattr(geocoding, "rg", None)


def test_reverse_geocoder_uses_offline_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    album = tmp_path / "album"
    album.mkdir()

    mock_search = MagicMock(return_value=[{"name": "Berlin", "admin1": "Berlin", "cc": "DE"}])
    monkeypatch.setattr(geocoding, "rg", SimpleNamespace(search=mock_search))

    geocoder = ReverseGeocoder.for_album(album)

    first = geocoder.lookup(52.52, 13.405)
    assert first == "Berlin, DE"
    mock_search.assert_called_once_with([(52.52, 13.405)])

    mock_search.reset_mock()
    second = geocoder.lookup(52.52, 13.405)
    assert second == "Berlin, DE"
    mock_search.assert_not_called()

    cache_file = album / ".iPhoto" / "geocache.json"
    assert cache_file.exists()


def test_reverse_geocoder_handles_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    album = tmp_path / "album"
    album.mkdir()

    mock_search = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(geocoding, "rg", SimpleNamespace(search=mock_search))

    geocoder = ReverseGeocoder.for_album(album)
    result = geocoder.lookup(10.0, 20.0)

    assert result is None
    mock_search.assert_called_once_with([(10.0, 20.0)])
