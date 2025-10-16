"""QOpenGLWidget based implementation that enables GPU accelerated rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QPointF, Signal
from PySide6.QtGui import QCloseEvent, QPainter, QResizeEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

from ._map_widget_base import MapWidgetController
from .map_renderer import CityAnnotation


class MapGLWidget(QOpenGLWidget):
    """Render the interactive preview using an OpenGL backed surface."""

    viewChanged = Signal(float, float, float)
    """Signal emitted whenever the map centre or zoom level changes."""

    panned = Signal(QPointF)
    """Signal emitted with raw drag deltas while the user drags the map."""

    panFinished = Signal()
    """Signal emitted once the current pan gesture completes."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        tile_root: Path | str = "tiles",
        style_path: Path | str = "style.json",
    ) -> None:
        super().__init__(parent)

        # ``MapWidgetController`` mirrors the logic used by the QWidget variant,
        # keeping rendering, tile loading, and input handling identical between
        # both front-ends while still giving this subclass full control over the
        # OpenGL specific surface lifecycle.
        self._controller = MapWidgetController(
            self,
            tile_root=tile_root,
            style_path=style_path,
        )
        self._controller.add_view_listener(self._emit_view_change)
        self._controller.add_pan_listener(self._emit_pan_delta)
        self._controller.add_pan_finished_listener(self._emit_pan_finished)

        # The OpenGL surface is now fully initialised, so QWidget-level helpers
        # such as mouse tracking and default sizing can be configured safely.
        self.setMouseTracking(True)
        self.setMinimumSize(640, 480)

    # ------------------------------------------------------------------
    @property
    def zoom(self) -> float:
        """Expose the current zoom level for the surrounding UI."""

        return self._controller.zoom

    # ------------------------------------------------------------------
    def set_zoom(self, zoom: float) -> None:
        """Forward zoom changes to the shared controller."""

        self._controller.set_zoom(zoom)

    # ------------------------------------------------------------------
    def reset_view(self) -> None:
        """Restore the default camera position and zoom level."""

        self._controller.reset_view()

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop background work before the widget is destroyed."""

        self._controller.shutdown()

    # ------------------------------------------------------------------
    def project_lonlat(self, lon: float, lat: float) -> QPointF | None:
        """Return widget-relative coordinates for the provided GPS point."""

        return self._controller.project_lonlat(lon, lat)

    # ------------------------------------------------------------------
    def center_on(self, lon: float, lat: float) -> None:
        """Centre the viewport on *lon*/*lat*."""

        self._controller.center_on(lon, lat)

    # ------------------------------------------------------------------
    def focus_on(self, lon: float, lat: float, zoom_delta: float = 1.0) -> None:
        """Centre the viewport on *lon*/*lat* and zoom by *zoom_delta*."""

        self._controller.focus_on(lon, lat, zoom_delta)

    # ------------------------------------------------------------------
    def set_city_annotations(self, cities: Sequence[CityAnnotation]) -> None:
        """Forward the supplied city annotations to the shared controller."""

        self._controller.set_cities(cities)

    # ------------------------------------------------------------------
    def city_at(self, position: QPointF) -> str | None:
        """Return the full label text for the city under ``position`` if any."""

        return self._controller.city_at(position)

    # ------------------------------------------------------------------
    def paintGL(self) -> None:  # type: ignore[override]
        """Render the current frame inside the active OpenGL context."""

        painter = QPainter()
        if not painter.begin(self):
            # ``begin`` can theoretically fail when the underlying context is no
            # longer valid.  Returning early keeps Qt from raising confusing
            # low-level exceptions.
            return

        try:
            self._controller.render(painter)
        finally:
            painter.end()

    # ------------------------------------------------------------------
    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        """Propagate resize events and notify listeners about the new viewport."""

        super().resizeEvent(event)
        self._emit_view_change(*self._controller.view_state())

    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Ensure worker threads shut down before the OpenGL surface disappears."""

        self.shutdown()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        """Forward mouse press events to the shared interaction handler."""

        self._controller.handle_mouse_press(event)
        super().mousePressEvent(event)

    # ------------------------------------------------------------------
    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        """Forward mouse move events to the shared interaction handler."""

        self._controller.handle_mouse_move(event)
        super().mouseMoveEvent(event)

    # ------------------------------------------------------------------
    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        """Forward mouse release events to the shared interaction handler."""

        self._controller.handle_mouse_release(event)
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    def wheelEvent(self, event) -> None:  # type: ignore[override]
        """Forward wheel events to the shared interaction handler."""

        self._controller.handle_wheel_event(event)
        super().wheelEvent(event)

    # ------------------------------------------------------------------
    def _emit_view_change(self, center_x: float, center_y: float, zoom: float) -> None:
        """Forward controller updates via the Qt signal for external consumers."""

        self.viewChanged.emit(float(center_x), float(center_y), float(zoom))

    # ------------------------------------------------------------------
    def _emit_pan_delta(self, delta: QPointF) -> None:
        """Proxy incremental drag deltas to consumers on the Qt signal."""

        self.panned.emit(QPointF(delta))

    # ------------------------------------------------------------------
    def _emit_pan_finished(self) -> None:
        """Notify listeners that the current drag gesture has ended."""

        self.panFinished.emit()


__all__ = ["MapGLWidget"]
