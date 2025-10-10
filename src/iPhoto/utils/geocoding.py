"""Reverse geocoding helpers with on-disk caching."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Dict, Optional

try:  # pragma: no cover - optional dependency failures handled at runtime
    from geopy.geocoders import Nominatim
except Exception:  # pragma: no cover - geopy missing or broken
    Nominatim = None  # type: ignore[assignment]

try:  # pragma: no cover - imported lazily when available
    from geopy.exc import GeopyError  # type: ignore
except Exception:  # pragma: no cover - geopy missing or broken
    GeopyError = Exception  # type: ignore[assignment]

from ..config import WORK_DIR_NAME
from .jsonio import atomic_write_text
from .pathutils import ensure_work_dir

CacheKey = str


def _normalise_key(lat: float, lon: float, *, precision: int = 4) -> CacheKey:
    """Return a stable cache key for *lat* and *lon* values."""

    return f"{round(lat, precision):.{precision}f},{round(lon, precision):.{precision}f}"


def _format_location(raw: object) -> Optional[str]:
    """Extract a human-friendly location name from a geopy result."""

    if raw is None:
        return None
    address = getattr(raw, "raw", {}).get("address") if hasattr(raw, "raw") else None
    if isinstance(address, dict):
        city = address.get("city") or address.get("town") or address.get("village")
        if not city:
            city = address.get("municipality") or address.get("county")
        district = (
            address.get("suburb")
            or address.get("neighbourhood")
            or address.get("city_district")
            or address.get("quarter")
        )
        state = address.get("state") or address.get("region")
        country = address.get("country")

        if city and district:
            return f"{city} - {district}"
        if city and state:
            return f"{city}, {state}"
        if city and country:
            return f"{city}, {country}"
        if state and country:
            return f"{state}, {country}"
        if city:
            return str(city)
        if country:
            return str(country)

    if hasattr(raw, "address") and isinstance(getattr(raw, "address"), str):
        return getattr(raw, "address")  # type: ignore[return-value]
    display = getattr(raw, "raw", {}).get("display_name") if hasattr(raw, "raw") else None
    if isinstance(display, str):
        return display
    return None


@dataclass
class ReverseGeocoder:
    """Reverse geocoding helper persisting lookups to the album cache."""

    cache_path: Path
    geolocator: Optional[object] = None
    user_agent: str = "iPhoto/0.1 (reverse-geocoder)"
    request_timeout: int = 5

    def __post_init__(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cache: Dict[CacheKey, str] = {}
        self._load_cache()
        if self.geolocator is None and Nominatim is not None:
            try:
                self.geolocator = Nominatim(user_agent=self.user_agent, timeout=self.request_timeout)
            except Exception:  # pragma: no cover - runtime failure to init geocoder
                self.geolocator = None

    # ------------------------------------------------------------------
    @classmethod
    def for_album(cls, album_root: Path, *, geolocator: Optional[object] = None) -> "ReverseGeocoder":
        """Return a geocoder instance for *album_root* using the shared cache."""

        cache_dir = ensure_work_dir(album_root, WORK_DIR_NAME)
        return cls(cache_dir / "geocache.json", geolocator=geolocator)

    # ------------------------------------------------------------------
    def lookup(self, latitude: float, longitude: float) -> Optional[str]:
        """Return a cached or freshly resolved label for *latitude*/*longitude*."""

        key = _normalise_key(latitude, longitude)
        with self._lock:
            cached = self._cache.get(key)
            if cached:
                return cached

        if self.geolocator is None:
            return None

        try:
            result = self.geolocator.reverse(  # type: ignore[call-arg]
                (latitude, longitude), exactly_one=True, language="en"
            )
        except GeopyError:
            return None
        except Exception:  # pragma: no cover - unexpected runtime failure
            return None

        label = _format_location(result)
        if not label:
            return None

        with self._lock:
            self._cache[key] = label
            self._flush_cache()
        return label

    # ------------------------------------------------------------------
    def _load_cache(self) -> None:
        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            self._cache = {}
            return
        except json.JSONDecodeError:
            self._cache = {}
            return
        except OSError:
            self._cache = {}
            return

        if isinstance(payload, dict):
            self._cache = {str(key): str(value) for key, value in payload.items() if value}
        else:
            self._cache = {}

    def _flush_cache(self) -> None:
        try:
            serialised = json.dumps(self._cache, ensure_ascii=False, sort_keys=True)
            atomic_write_text(self.cache_path, serialised + "\n")
        except OSError:  # pragma: no cover - disk errors surfaced in logs
            pass


__all__ = ["ReverseGeocoder"]

