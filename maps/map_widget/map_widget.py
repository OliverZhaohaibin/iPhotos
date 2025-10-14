"""QWidget based implementation of the interactive map preview widget."""

from __future__ import annotations

from PySide6.QtGui import QCloseEvent, QPainter
from PySide6.QtWidgets import QWidget

from ._map_widget_base import MapWidgetController


class MapWidget(QWidget):
    """Display an interactive preview using the traditional QWidget surface."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        tile_root: str = "tiles",
        style_path: str = "style.json",
    ) -> None:
        super().__init__(parent)

        # ``MapWidgetController`` owns the heavy lifting (tile loading, rendering
        # setup, and gesture handling) so this subclass focuses solely on the
        # QWidget specific concerns.
        self._controller = MapWidgetController(
            self,
            tile_root=tile_root,
            style_path=style_path,
        )

        # QWidget level convenience helpers are safe to call now that the base
        # class finished initialising and the controller has attached to the
        # widget.
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
    def paintEvent(self, event) -> None:  # type: ignore[override]
        """Render the current scene using CPU backed ``QPainter`` drawing."""

        painter = QPainter(self)
        try:
            self._controller.render(painter)
        finally:
            painter.end()

    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Tear down background threads before the widget is destroyed."""

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


__all__ = ["MapWidget"]
