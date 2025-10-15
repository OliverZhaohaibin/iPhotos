"""Shared controller utilities for the QWidget and QOpenGLWidget map views."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Protocol

from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QPainter

from iPhotos.maps.style_resolver import StyleLoadError, StyleResolver
from iPhotos.maps.tile_parser import TileParser

from .input_handler import InputHandler
from .layer import LayerPlan
from .map_renderer import MapRenderer
from .tile_manager import TileManager


class SupportsMapViewport(Protocol):
    """Minimal interface the rendering controller expects from the widget."""

    def update(self) -> None:  # pragma: no cover - interface definition only
        ...

    def width(self) -> int:  # pragma: no cover - interface definition only
        ...

    def height(self) -> int:  # pragma: no cover - interface definition only
        ...

    def setCursor(self, cursor) -> None:  # pragma: no cover - cursor type provided by Qt
        ...

    def unsetCursor(self) -> None:  # pragma: no cover - cursor type provided by Qt
        ...

    def setMouseTracking(self, enabled: bool) -> None:  # pragma: no cover - Qt helper
        ...

    def setMinimumSize(self, width: int, height: int) -> None:  # pragma: no cover - Qt helper
        ...


class MapWidgetBase(Protocol):
    """Structural typing hook used by ``main.py`` for widget factories."""

    @property
    def zoom(self) -> float:  # pragma: no cover - interface definition only
        ...

    def set_zoom(self, zoom: float) -> None:  # pragma: no cover - interface definition only
        ...

    def reset_view(self) -> None:  # pragma: no cover - interface definition only
        ...

    def shutdown(self) -> None:  # pragma: no cover - interface definition only
        ...


class MapWidgetController:
    """Encapsulate rendering, tile management, and input handling logic."""

    TILE_SIZE = 256
    MIN_ZOOM = 0.0
    MAX_ZOOM = 8.0

    def __init__(
        self,
        widget: SupportsMapViewport,
        *,
        tile_root: Path | str = "tiles",
        style_path: Path | str = "style.json",
    ) -> None:
        """Prepare long-lived helpers shared by both widget implementations.

        The heavy lifting—style resolution, tile loading, and gesture
        interpretation—does not depend on whether the caller renders via a plain
        :class:`~PySide6.QtWidgets.QWidget` or a
        :class:`~PySide6.QtOpenGLWidgets.QOpenGLWidget`.  Centralising the logic
        in this controller keeps both widget front-ends perfectly in sync while
        ensuring each helper ``QObject`` uses the fully constructed widget as
        its parent.
        """

        self._widget = widget
        self._view_listeners: list[Callable[[float, float, float], None]] = []
        self._pan_listeners: list[Callable[[QPointF], None]] = []
        self._pan_finished_listeners: list[Callable[[], None]] = []

        package_root = Path(__file__).resolve().parent.parent

        # ``PhotoMapView`` embeds the widget from inside the main desktop
        # application where the current working directory differs from the
        # standalone demo in ``maps/main.py``.  Resolving the asset paths against
        # the package directory keeps the map functional regardless of where the
        # process was started.
        tile_root_path = Path(tile_root)
        if not tile_root_path.is_absolute():
            tile_root_path = package_root / tile_root_path

        style_path_obj = Path(style_path)
        if not style_path_obj.is_absolute():
            style_path_obj = package_root / style_path_obj

        # ``TileParser`` performs the expensive vector tile decoding while the
        # style resolver exposes drawing instructions taken from ``style.json``.
        self._tile_parser = TileParser(tile_root_path)
        self._style = StyleResolver(style_path_obj)

        definitions = self._style.vector_layer_definitions()
        if not definitions:
            raise StyleLoadError(
                "The style file does not define any vector layers that the preview can render",
            )

        self._layers: list[LayerPlan] = [
            LayerPlan(
                definition["source_layer"],
                definition["style_layer"],
                definition["kind"],
                is_lonlat=bool(definition.get("is_lonlat", False)),
            )
            for definition in definitions
        ]

        # Parenting the helper ``QObject`` instances to the widget guarantees
        # they share its lifetime and prevents premature destruction when the
        # surrounding UI is replaced.
        self._tile_manager = TileManager(self._tile_parser, cache_limit=256, parent=self._widget)
        self._renderer = MapRenderer(
            style=self._style,
            tile_manager=self._tile_manager,
            layers=self._layers,
            tile_size=self.TILE_SIZE,
        )
        self._input_handler = InputHandler(
            min_zoom=self.MIN_ZOOM,
            max_zoom=self.MAX_ZOOM,
            parent=self._widget,
        )

        self._tile_manager.tile_loaded.connect(self._handle_tile_loaded)
        self._tile_manager.tile_missing.connect(self._handle_tile_missing)
        self._tile_manager.tile_removed.connect(self._handle_tile_removed)
        self._tile_manager.tiles_changed.connect(self._schedule_update)

        self._input_handler.pan_requested.connect(self._on_pan_requested)
        self._input_handler.pan_requested.connect(self._notify_pan_delta)
        self._input_handler.pan_finished.connect(self._notify_pan_finished)
        self._input_handler.zoom_requested.connect(self._on_zoom_requested)
        self._input_handler.cursor_changed.connect(self._widget.setCursor)
        self._input_handler.cursor_reset.connect(self._widget.unsetCursor)

        self._update_timer = QTimer(self._widget)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(16)
        self._update_timer.timeout.connect(self._widget.update)

        self._center_x = 0.5
        self._center_y = 0.5
        self._zoom = 2.0

    # ------------------------------------------------------------------
    @property
    def zoom(self) -> float:
        """Expose the current zoom level for UI elements such as the title bar."""

        return self._zoom

    # ------------------------------------------------------------------
    def set_zoom(self, zoom: float) -> None:
        """Clamp ``zoom`` to the supported range and schedule a repaint."""

        zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))
        if zoom == self._zoom:
            return
        self._zoom = zoom
        self._widget.update()
        self._notify_view_changed()

    # ------------------------------------------------------------------
    def reset_view(self) -> None:
        """Re-centre the map and restore the default zoom level."""

        self._center_x = 0.5
        self._center_y = 0.5
        self.set_zoom(2.0)
        self._widget.update()
        self._notify_view_changed()

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop the tile loader thread so the application can exit cleanly."""

        self._tile_manager.shutdown()

    # ------------------------------------------------------------------
    def render(self, painter: QPainter) -> None:
        """Draw the current frame into ``painter`` using MapLibre styling."""

        painter.setRenderHint(QPainter.Antialiasing, True)
        self._renderer.render(
            painter,
            center_x=self._center_x,
            center_y=self._center_y,
            zoom=self._zoom,
            width=self._widget.width(),
            height=self._widget.height(),
        )

    # ------------------------------------------------------------------
    def handle_mouse_press(self, event) -> None:
        """Delegate mouse press events to the shared input handler."""

        self._input_handler.handle_mouse_press(event)

    # ------------------------------------------------------------------
    def handle_mouse_move(self, event) -> None:
        """Delegate mouse move events to the shared input handler."""

        self._input_handler.handle_mouse_move(event)

    # ------------------------------------------------------------------
    def handle_mouse_release(self, event) -> None:
        """Delegate mouse release events to the shared input handler."""

        self._input_handler.handle_mouse_release(event)

    # ------------------------------------------------------------------
    def handle_wheel_event(self, event) -> None:
        """Delegate wheel events to the shared input handler."""

        self._input_handler.handle_wheel_event(event, self._zoom)

    # ------------------------------------------------------------------
    def add_view_listener(self, callback: Callable[[float, float, float], None]) -> None:
        """Register *callback* to receive camera updates."""

        if callback not in self._view_listeners:
            self._view_listeners.append(callback)

    # ------------------------------------------------------------------
    def add_pan_listener(self, callback: Callable[[QPointF], None]) -> None:
        """Register *callback* for raw drag deltas produced by the input handler."""

        if callback not in self._pan_listeners:
            self._pan_listeners.append(callback)

    # ------------------------------------------------------------------
    def add_pan_finished_listener(self, callback: Callable[[], None]) -> None:
        """Register *callback* to be notified once a drag gesture completes."""

        if callback not in self._pan_finished_listeners:
            self._pan_finished_listeners.append(callback)

    # ------------------------------------------------------------------
    def project_lonlat(self, lon: float, lat: float) -> QPointF | None:
        """Return widget-relative coordinates for the provided GPS point."""

        world_position = self._lonlat_to_world(lon, lat)
        if world_position is None:
            return None

        world_x, world_y = world_position
        world_size = self._world_size()

        center_px = self._center_x * world_size
        center_py = self._center_y * world_size

        delta_x = world_x - center_px
        if delta_x > world_size / 2.0:
            world_x -= world_size
        elif delta_x < -world_size / 2.0:
            world_x += world_size

        top_left_x = center_px - self._widget.width() / 2.0
        top_left_y = center_py - self._widget.height() / 2.0

        screen_x = world_x - top_left_x
        screen_y = world_y - top_left_y
        return QPointF(screen_x, screen_y)

    # ------------------------------------------------------------------
    def center_on(self, lon: float, lat: float) -> None:
        """Move the camera so *lon*/*lat* becomes the viewport centre."""

        world_position = self._lonlat_to_world(lon, lat)
        if world_position is None:
            return
        world_x, world_y = world_position
        world_size = self._world_size()
        self._center_x = (world_x / world_size) % 1.0
        self._center_y = min(max(world_y / world_size, 0.0), 1.0)
        self._widget.update()
        self._notify_view_changed()

    # ------------------------------------------------------------------
    def focus_on(self, lon: float, lat: float, zoom_delta: float = 1.0) -> None:
        """Centre the camera on *lon*/*lat* and optionally increase zoom."""

        self.center_on(lon, lat)
        if zoom_delta:
            self.set_zoom(self._zoom + zoom_delta)

    # ------------------------------------------------------------------
    def view_state(self) -> tuple[float, float, float]:
        """Return the current ``(center_x, center_y, zoom)`` tuple."""

        return self._center_x, self._center_y, self._zoom

    # ------------------------------------------------------------------
    def _schedule_update(self) -> None:
        """Start the coalescing timer when new tiles arrive."""

        if not self._update_timer.isActive():
            self._update_timer.start()

    # ------------------------------------------------------------------
    def _on_pan_requested(self, delta: QPointF) -> None:
        """Translate drag gestures from screen space to world space."""

        world_size = self._world_size()
        self._center_x -= delta.x() / world_size
        self._center_y -= delta.y() / world_size
        self._wrap_center()
        self._widget.update()
        self._notify_view_changed()

    # ------------------------------------------------------------------
    def _notify_pan_delta(self, delta: QPointF) -> None:
        """Forward the on-screen drag delta to registered observers."""

        for callback in list(self._pan_listeners):
            try:
                callback(delta)
            except Exception:  # pragma: no cover - observers are best effort only
                continue

    # ------------------------------------------------------------------
    def _notify_pan_finished(self) -> None:
        """Notify observers that the current pan gesture has concluded."""

        for callback in list(self._pan_finished_listeners):
            try:
                callback()
            except Exception:  # pragma: no cover - observers are best effort only
                continue

    # ------------------------------------------------------------------
    def _on_zoom_requested(self, new_zoom: float, anchor: QPointF) -> None:
        """Zoom around ``anchor`` to keep the cursor position fixed."""

        world_size = self._world_size()
        center_px = self._center_x * world_size
        center_py = self._center_y * world_size
        view_top_left_x = center_px - self._widget.width() / 2.0
        view_top_left_y = center_py - self._widget.height() / 2.0

        mouse_world_x = (view_top_left_x + anchor.x()) / world_size
        mouse_world_y = (view_top_left_y + anchor.y()) / world_size

        self._zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, new_zoom))
        new_world_size = self._world_size()
        new_center_px = mouse_world_x * new_world_size - anchor.x() + self._widget.width() / 2.0
        new_center_py = mouse_world_y * new_world_size - anchor.y() + self._widget.height() / 2.0

        self._center_x = new_center_px / new_world_size
        self._center_y = new_center_py / new_world_size
        self._wrap_center()
        self._widget.update()
        self._notify_view_changed()

    # ------------------------------------------------------------------
    def _handle_tile_loaded(self, tile_key: tuple[int, int, int]) -> None:
        """Invalidate cached geometry when fresh tile data arrives."""

        self._renderer.invalidate_tile(tile_key)

    # ------------------------------------------------------------------
    def _handle_tile_missing(self, tile_key: tuple[int, int, int]) -> None:
        """Forget cached geometry for tiles that failed to load."""

        self._renderer.invalidate_tile(tile_key)

    # ------------------------------------------------------------------
    def _handle_tile_removed(self, tile_key: tuple[int, int, int]) -> None:
        """Drop cached geometry when a tile leaves the cache."""

        self._renderer.invalidate_tile(tile_key)

    # ------------------------------------------------------------------
    def _world_size(self) -> float:
        """Compute the virtual map size in pixels at the current zoom level."""

        return float(self.TILE_SIZE * (2 ** self._zoom))

    # ------------------------------------------------------------------
    def _wrap_center(self) -> None:
        """Ensure the virtual camera remains within sensible bounds."""

        self._center_x %= 1.0
        self._center_y = min(max(self._center_y, 0.0), 1.0)

    # ------------------------------------------------------------------
    def _notify_view_changed(self) -> None:
        """Emit the current view state to registered listeners."""

        for callback in list(self._view_listeners):
            try:
                callback(self._center_x, self._center_y, self._zoom)
            except Exception:  # pragma: no cover - best effort notification
                continue

    # ------------------------------------------------------------------
    def _lonlat_to_world(self, lon: float, lat: float) -> tuple[float, float] | None:
        """Project GPS coordinates into the continuous Web Mercator plane."""

        try:
            lon = float(lon)
            lat = max(min(float(lat), 85.05112878), -85.05112878)
        except (TypeError, ValueError):
            return None

        world_size = self._world_size()
        x = (lon + 180.0) / 360.0 * world_size
        sin_lat = math.sin(math.radians(lat))
        y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * world_size
        return x, y


__all__ = ["MapWidgetBase", "MapWidgetController", "SupportsMapViewport"]
