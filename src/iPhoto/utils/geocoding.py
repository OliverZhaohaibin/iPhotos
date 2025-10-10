"""Reverse geocoding helpers with on-disk caching."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, Optional

try:  # pragma: no cover - optional dependency handled at runtime
    import reverse_geocoder as rg
except ImportError:  # pragma: no cover - dependency missing in environment
    rg = None  # type: ignore[assignment]

from ..config import WORK_DIR_NAME
from .jsonio import atomic_write_text
from .logging import get_logger
from .pathutils import ensure_work_dir

LOGGER = get_logger()
CacheKey = str


def _normalise_key(lat: float, lon: float, *, precision: int = 4) -> CacheKey:
    """Return a stable cache key for *lat* and *lon* values."""

    return f"{round(lat, precision):.{precision}f},{round(lon, precision):.{precision}f}"


def _format_local_location(data: Dict[str, object]) -> Optional[str]:
    """Extract a human-friendly location name from a reverse_geocoder result."""

    city = data.get("name")
    admin1 = data.get("admin1")
    country = data.get("cc")

    if not isinstance(city, str) or not city:
        return None

    if isinstance(admin1, str) and admin1 and admin1 != city:
        return f"{city}, {admin1}"
    if isinstance(country, str) and country:
        return f"{city}, {country}"
    return city


@dataclass
class ReverseGeocoder:
    """Reverse geocoding helper persisting lookups to the album cache."""

    cache_path: Path
    _warned_missing: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cache: Dict[CacheKey, str] = {}
        self._load_cache()

    # ------------------------------------------------------------------
    @classmethod
    def for_album(cls, album_root: Path) -> "ReverseGeocoder":
        """Return a geocoder instance for *album_root* using the shared cache."""

        cache_dir = ensure_work_dir(album_root, WORK_DIR_NAME)
        return cls(cache_dir / "geocache.json")

    # ------------------------------------------------------------------
    def lookup(self, latitude: float, longitude: float) -> Optional[str]:
        """Return a cached or freshly resolved label for *latitude*/*longitude*."""

        key = _normalise_key(latitude, longitude)
        with self._lock:
            cached = self._cache.get(key)
            if cached:
                return cached

        if rg is None:
            if not self._warned_missing:
                LOGGER.warning(
                    "reverse-geocoder dependency is not available; location lookups are disabled."
                )
                self._warned_missing = True
            return None

        try:
            results = rg.search([(latitude, longitude)])
        except Exception as exc:  # pragma: no cover - surfaced in logs
            LOGGER.error("Offline reverse geocoding failed: %s", exc, exc_info=True)
            return None

        if not results:
            return None

        label = _format_local_location(results[0])
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
        except OSError as exc:
            LOGGER.error("Failed to read geocoding cache: %s", exc)
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
        except OSError as exc:  # pragma: no cover - disk errors surfaced in logs
            LOGGER.error("Failed to write geocoding cache: %s", exc)


__all__ = ["ReverseGeocoder"]
