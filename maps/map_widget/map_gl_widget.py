"""QOpenGLWidget based implementation that enables GPU accelerated rendering."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QCloseEvent, QPainter
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

from ._map_widget_base import MapWidgetController


class MapGLWidget(QOpenGLWidget):
    """Render the interactive preview using an OpenGL backed surface."""

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


__all__ = ["MapGLWidget"]
