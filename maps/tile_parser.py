"""Utility classes for loading Mapbox vector tiles from the local filesystem.

The module exposes :class:`TileParser`, a small helper that reads ``.pbf``
vector tile files produced by MapTiler.  The files in this repository use the
TMS tile scheme, so the parser contains convenience helpers for converting the
requested XYZ tile coordinates to the on-disk TMS path.  The parser also
provides a tiny LRU cache so we do not have to decode the same tile repeatedly
while panning the map.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Dict, Optional

import mapbox_vector_tile


class TileLoadingError(Exception):
    """Base exception for recoverable tile loading problems."""


class TileAccessError(TileLoadingError):
    """Raised when a tile exists on disk but cannot be read."""


class TileDecodeError(TileLoadingError):
    """Raised when ``mapbox_vector_tile`` fails to decode the payload."""


class TileParser:
    """Load and decode Mapbox vector tiles from a folder hierarchy.

    Parameters
    ----------
    tile_root:
        Path to the folder that contains the ``{z}/{x}/{y}.pbf`` hierarchy.
    cache_size:
        Maximum number of decoded tiles to retain in memory.  The defaults work
        well for interactive preview purposes where the user is only looking at
        a handful of tiles at a time.
    """

    def __init__(self, tile_root: Path | str, cache_size: int = 512) -> None:
        self.tile_root = Path(tile_root)
        if not self.tile_root.exists():
            raise TileAccessError(f"Tile directory '{self.tile_root}' does not exist")
        if not self.tile_root.is_dir():
            raise TileAccessError(f"Tile path '{self.tile_root}' is not a directory")
        self._cached_loader = lru_cache(maxsize=cache_size)(self._load_tile)
        # ``functools.lru_cache`` is not thread-safe by default.  The lightweight
        # lock ensures that decoding requests coming from worker threads do not
        # corrupt the cache state when multiple tiles are requested
        # simultaneously.
        self._lock = Lock()

    # ------------------------------------------------------------------
    def load_tile(self, z: int, x: int, y: int) -> Optional[Dict[str, dict]]:
        """Return the decoded tile for the requested XYZ coordinates."""

        # Returning ``None`` when a tile is missing preserves the original
        # behavior of the preview application.  Recoverable I/O or decoding
        # issues are surfaced as :class:`TileLoadingError` so callers can log
        # diagnostics without catching unrelated exceptions.

        with self._lock:
            return self._cached_loader(z, x, y)

    # ------------------------------------------------------------------
    def clear_cache(self) -> None:
        """Completely empty the internal cache.

        This is primarily useful for debugging or when the tile set changes on
        disk while the application is running.
        """

        with self._lock:
            self._cached_loader.cache_clear()

    # ------------------------------------------------------------------
    def _load_tile(self, z: int, x: int, y: int) -> Optional[Dict[str, dict]]:
        """Read and decode the vector tile from disk."""

        path = self._resolve_tile_path(z, x, y)
        if path is None or not path.exists():
            return None

        try:
            data = path.read_bytes()
        except OSError as exc:
            raise TileAccessError(f"Unable to read tile {z}/{x}/{y} from disk") from exc

        try:
            return mapbox_vector_tile.decode(data)
        except Exception as exc:  # pragma: no cover - passthrough for third-party errors
            raise TileDecodeError(f"Failed to decode tile {z}/{x}/{y}") from exc

    # ------------------------------------------------------------------
    def _resolve_tile_path(self, z: int, x: int, y: int) -> Optional[Path]:
        """Translate XYZ coordinates into the on-disk TMS path.

        MapTiler exports tiles in the TMS scheme where the Y axis is flipped
        compared to the WebMercator XYZ layout.  The helper converts between
        the two and falls back to a secondary naming pattern that includes
        negative indices.  The latter is present in this repository for a small
        subset of tiles and is handled here for completeness.
        """

        if z < 0:
            return None

        n = 1 << z
        x_wrapped = x % n
        if y < 0 or y >= n:
            return None

        # Convert XYZ to TMS by flipping the Y axis.
        tms_y = (n - 1) - y

        base = self.tile_root / str(z) / str(x_wrapped)
        primary = base / f"{tms_y}.pbf"
        if primary.exists():
            return primary

        # MapTiler optionally writes additional files using negative indices;
        # these represent wrapped tiles.  We only touch them when the regular
        # path is missing.
        secondary = base / f"{y - n}.pbf"
        if secondary.exists():
            return secondary

        return primary


__all__ = [
    "TileParser",
    "TileLoadingError",
    "TileAccessError",
    "TileDecodeError",
]
