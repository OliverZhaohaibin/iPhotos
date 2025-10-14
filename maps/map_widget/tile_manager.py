"""Background tile loading and caching infrastructure."""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Iterable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from tile_parser import TileLoadingError, TileParser


class _TileWorker(QObject):
    """Background worker that retrieves tiles without blocking the GUI."""

    tile_loaded = Signal(int, int, int, object)
    tile_missing = Signal(int, int, int)

    def __init__(self, tile_parser: TileParser) -> None:
        super().__init__()
        self._tile_parser = tile_parser

    @Slot(int, int, int)
    def request_tile(self, z: int, x: int, y: int) -> None:
        """Load a tile inside the worker thread and report the outcome."""

        try:
            tile = self._tile_parser.load_tile(z, x, y)
        except TileLoadingError as exc:
            logging.getLogger(__name__).warning(
                "Tile %s/%s/%s could not be loaded: %s",
                z,
                x,
                y,
                exc,
            )
            self.tile_missing.emit(z, x, y)
            return

        if tile is None:
            self.tile_missing.emit(z, x, y)
            return

        self.tile_loaded.emit(z, x, y, tile)


class TileManager(QObject):
    """Manage tile loading, caching, and worker thread lifecycle."""

    tile_loaded = Signal(tuple)
    tile_missing = Signal(tuple)
    tile_removed = Signal(tuple)
    tiles_changed = Signal()

    _request_tile = Signal(int, int, int)

    def __init__(
        self,
        tile_parser: TileParser,
        *,
        cache_limit: int = 256,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._tile_parser = tile_parser
        self._cache_limit = cache_limit

        self._tile_cache: OrderedDict[tuple[int, int, int], dict] = OrderedDict()
        self._pending_tiles: set[tuple[int, int, int]] = set()
        self._missing_tiles: set[tuple[int, int, int]] = set()

        self._loader_thread: QThread | None = QThread(self)
        self._tile_worker = _TileWorker(self._tile_parser)
        self._tile_worker.moveToThread(self._loader_thread)
        self._tile_worker.tile_loaded.connect(self._handle_tile_loaded)
        self._tile_worker.tile_missing.connect(self._handle_tile_missing)
        self._request_tile.connect(self._tile_worker.request_tile)
        self._loader_thread.finished.connect(self._tile_worker.deleteLater)
        self._loader_thread.start()

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop the background worker thread and release resources."""

        if self._loader_thread is None:
            return

        if self._loader_thread.isRunning():
            self._loader_thread.quit()
            self._loader_thread.wait()

        self._loader_thread = None

    # ------------------------------------------------------------------
    def get_tile(self, tile_key: tuple[int, int, int]) -> dict | None:
        """Return a cached tile, updating the LRU ordering when found."""

        tile = self._tile_cache.get(tile_key)
        if tile is not None:
            self._tile_cache.move_to_end(tile_key)
        return tile

    # ------------------------------------------------------------------
    def ensure_tile(self, tile_key: tuple[int, int, int]) -> None:
        """Schedule ``tile_key`` for loading when it is not cached."""

        if tile_key in self._missing_tiles or tile_key in self._pending_tiles:
            return

        self._pending_tiles.add(tile_key)
        self._request_tile.emit(*tile_key)

    # ------------------------------------------------------------------
    def is_tile_missing(self, tile_key: tuple[int, int, int]) -> bool:
        """Return ``True`` when ``tile_key`` previously failed to load."""

        return tile_key in self._missing_tiles

    # ------------------------------------------------------------------
    def pending_tiles(self) -> Iterable[tuple[int, int, int]]:
        """Expose the set of in-flight requests for diagnostics/testing."""

        return set(self._pending_tiles)

    # ------------------------------------------------------------------
    def _handle_tile_loaded(self, z: int, x: int, y: int, tile: dict) -> None:
        """Store the freshly loaded tile and emit update signals."""

        key = (z, x, y)
        self._pending_tiles.discard(key)
        self._missing_tiles.discard(key)
        self._tile_cache[key] = tile
        self._tile_cache.move_to_end(key)
        self.tile_loaded.emit(key)

        while len(self._tile_cache) > self._cache_limit:
            evicted_key, _ = self._tile_cache.popitem(last=False)
            self.tile_removed.emit(evicted_key)

        self.tiles_changed.emit()

    # ------------------------------------------------------------------
    def _handle_tile_missing(self, z: int, x: int, y: int) -> None:
        """Remember that a tile is unavailable and notify listeners."""

        key = (z, x, y)
        self._pending_tiles.discard(key)
        self._missing_tiles.add(key)
        if key in self._tile_cache:
            del self._tile_cache[key]
            self.tile_removed.emit(key)
        self.tile_missing.emit(key)
        self.tiles_changed.emit()


__all__ = ["TileManager"]
