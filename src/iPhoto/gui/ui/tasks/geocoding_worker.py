"""Background helpers for reverse geocoding tasks."""

from __future__ import annotations

from threading import Lock
from typing import Callable, Optional

from PySide6.QtCore import QRunnable, QThreadPool

from ....utils.geocoding import ReverseGeocoder


class GeocodingWorker(QRunnable):
    """Resolve a human readable label for a set of GPS coordinates."""

    def __init__(
        self,
        geocoder: ReverseGeocoder | None,
        rel: str,
        latitude: float,
        longitude: float,
        callback: Callable[[str, Optional[str]], None],
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._geocoder = geocoder
        self._rel = rel
        self._latitude = latitude
        self._longitude = longitude
        self._callback = callback

    def run(self) -> None:  # pragma: no cover - executed on worker threads
        location: Optional[str] = None
        if self._geocoder is not None:
            location = self._geocoder.lookup(self._latitude, self._longitude)
        self._callback(self._rel, location)


_geocode_pool: QThreadPool | None = None
_pool_lock = Lock()


def geocoding_pool() -> QThreadPool:
    """Return the shared thread pool used for geocoding work."""

    global _geocode_pool
    with _pool_lock:
        if _geocode_pool is None:
            pool = QThreadPool()
            pool.setMaxThreadCount(1)
            _geocode_pool = pool
    assert _geocode_pool is not None
    return _geocode_pool


__all__ = ["GeocodingWorker", "geocoding_pool"]
