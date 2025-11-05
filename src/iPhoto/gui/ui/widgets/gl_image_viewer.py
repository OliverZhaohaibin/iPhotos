"""GPU-accelerated image viewer that uses an offscreen renderer."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QImage, QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtOpenGL import QOpenGLTexture
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..palette import viewer_surface_color
from .offscreen_gl_renderer import OffscreenGLRenderer


class GLImageViewer(QWidget):
    """A QWidget that displays GPU-rendered images from an offscreen context."""

    replayRequested = Signal()
    zoomChanged = Signal(float)
    nextItemRequested = Signal()
    prevItemRequested = Signal()
    fullscreenExitRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._renderer = OffscreenGLRenderer()
        self._label = QLabel(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)
        self.setStyleSheet(f"background-color: {viewer_surface_color(self)};")

        self._image: Optional[QImage] = None
        self._texture: Optional[QOpenGLTexture] = None
        self._adjustments: dict = {}
        self._zoom_factor = 1.0
        self._min_zoom = 0.1
        self._max_zoom = 4.0
        self._pan_offset = QPointF(0, 0)
        self._is_panning = False
        self._pan_start_pos = QPointF()
        self._wheel_action = "navigate"
        self._live_replay_enabled = False

        self.reset_zoom()

    def shutdown(self) -> None:
        """Clean up the offscreen renderer's resources."""
        if self._texture:
            self._texture.destroy()
        self._renderer.shutdown()

    def set_image(self, image: Optional[QImage], adjustments: dict[str, float]) -> None:
        """Set the image to be displayed."""
        self._image = image
        self._adjustments = adjustments
        if self._texture:
            self._texture.destroy()
            self._texture = None
        if self._image and not self._image.isNull():
            self._texture = self._renderer.create_texture(self._image)
        self.reset_zoom()
        self._schedule_update()

    def set_placeholder(self, pixmap: Optional[QPixmap]) -> None:
        """Set a placeholder pixmap to be displayed while the full image loads."""
        if pixmap and not pixmap.isNull():
            self._label.setPixmap(pixmap)
        else:
            self._label.clear()

    def set_live_replay_enabled(self, enabled: bool) -> None:
        """Allow emitting replay requests when the still frame is shown."""
        self._live_replay_enabled = bool(enabled)

    def set_wheel_action(self, action: str) -> None:
        """Control how the viewer reacts to wheel gestures."""
        self._wheel_action = "zoom" if action == "zoom" else "navigate"

    def set_zoom(self, factor: float, anchor: Optional[QPointF] = None) -> None:
        """Set the zoom *factor* relative to the fit-to-window baseline."""
        clamped = max(self._min_zoom, min(self._max_zoom, float(factor)))
        if abs(clamped - self._zoom_factor) < 1e-3:
            return
        self._zoom_factor = clamped
        self._schedule_update()
        self.zoomChanged.emit(self._zoom_factor)

    def reset_zoom(self) -> None:
        """Return the zoom factor to ``1.0`` (fit to window)."""
        self._zoom_factor = 1.0
        self._pan_offset = QPointF(0, 0)
        self._schedule_update()
        self.zoomChanged.emit(self._zoom_factor)

    def zoom_in(self) -> None:
        """Increase the zoom factor using a standard step."""
        self.set_zoom(self._zoom_factor + 0.1)

    def zoom_out(self) -> None:
        """Decrease the zoom factor using a standard step."""
        self.set_zoom(self._zoom_factor - 0.1)

    def viewport_center(self) -> QPointF:
        """Return the centre point of the widget."""
        return QPointF(self.width() / 2, self.height() / 2)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._live_replay_enabled:
                self.replayRequested.emit()
            else:
                self._is_panning = True
                self._pan_start_pos = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_panning:
            delta = event.position() - self._pan_start_pos
            self._pan_start_pos = event.position()
            pan_delta_x = 2.0 * delta.x() / self.width()
            pan_delta_y = -2.0 * delta.y() / self.height()
            self._pan_offset += QPointF(pan_delta_x, pan_delta_y)
            self._schedule_update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_panning = False
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.fullscreenExitRequested.emit()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Handle wheel events for zooming or navigation."""
        if self._wheel_action == "zoom":
            angle = event.angleDelta().y()
            if angle > 0:
                self.zoom_in()
            elif angle < 0:
                self.zoom_out()
        else:
            delta = event.angleDelta()
            step = delta.y() or delta.x()
            if step < 0:
                self.nextItemRequested.emit()
            elif step > 0:
                self.prevItemRequested.emit()
        event.accept()

    def resizeEvent(self, event: QWheelEvent) -> None:
        """Schedule a render when the widget is resized."""
        super().resizeEvent(event)
        self._schedule_update()

    def _schedule_update(self) -> None:
        """Schedule a rendering pass in the next event loop cycle."""
        QTimer.singleShot(0, self._render_and_display)

    def _render_and_display(self) -> None:
        """Render the image with adjustments and display it on the label."""
        if not self._texture or not self.isVisible():
            return

        label_size = self._label.size()
        if not label_size.isValid():
            return

        rendered_qimage = self._renderer.render(
            label_size,
            self._texture,
            self._adjustments,
            self._zoom_factor,
            self._pan_offset,
        )
        self._label.setPixmap(QPixmap.fromImage(rendered_qimage))
