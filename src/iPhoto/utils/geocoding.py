"""Helpers for reverse geocoding GPS coordinates."""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, Optional

import reverse_geocoder  # type: ignore[import]


@lru_cache(maxsize=1)
def _geocoder() -> "reverse_geocoder.RGeocoder":
    """Return a cached reverse geocoder instance."""

    return reverse_geocoder.RGeocoder(mode=1, verbose=False)


def resolve_location_name(gps: Optional[Dict[str, float]]) -> Optional[str]:
    """Return a human readable place name for *gps* coordinates.

    Parameters
    ----------
    gps:
        Mapping containing ``lat`` and ``lon`` keys. When either value is
        missing or the lookup fails the function returns ``None``.
    """

    if not gps:
        return None
    latitude = gps.get("lat")
    longitude = gps.get("lon")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None

    try:
        result = _geocoder().query([(latitude, longitude)])
    except Exception:
        return None

    record: Optional[Dict[str, str]] = None
    def _to_text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

    if isinstance(result, dict):
        record = {key: _to_text(value) for key, value in result.items() if isinstance(key, str)}
    elif isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            record = {
                key: _to_text(value)
                for key, value in first.items()
                if isinstance(key, str)
            }

    if not record:
        return None

    city = str(record.get("name", "")).strip()
    admin = str(record.get("admin2") or record.get("admin1") or "").strip()

    components = [component for component in (city, admin) if component]
    if not components:
        return None
    # Use an en dash to match macOS Photos' layout conventions.
    return " — ".join(components)


__all__ = ["resolve_location_name"]

