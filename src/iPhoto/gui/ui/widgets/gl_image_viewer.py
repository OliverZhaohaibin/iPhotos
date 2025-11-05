"""GPU-accelerated image viewer."""

from __future__ import annotations

import numpy as np
from typing import Optional

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPixmap, QWheelEvent, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFunctions_3_3_Core,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget


from ..palette import viewer_surface_color
from .shaders import FRAGMENT_SHADER, VERTEX_SHADER


class GLImageViewer(QOpenGLWidget):
    """A QWidget that displays GPU-rendered images."""

    replayRequested = Signal()
    zoomChanged = Signal(float)
    nextItemRequested = Signal()
    prevItemRequested = Signal()
    fullscreenExitRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)

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
        """Clean up the renderer's resources."""
        self.makeCurrent()
        if self._texture:
            self._texture.destroy()
        self.doneCurrent()

    def set_image(self, image: Optional[QImage], adjustments: dict[str, float]) -> None:
        """Set the image to be displayed."""
        self.makeCurrent()
        self._image = image
        self._adjustments = adjustments
        if self._texture:
            self._texture.destroy()
            self._texture = None
        if self._image and not self._image.isNull():
            self._texture = QOpenGLTexture(self._image.mirrored())
        self.doneCurrent()
        self.reset_zoom()
        self.update()

    def set_placeholder(self, pixmap: Optional[QPixmap]) -> None:
        """Set a placeholder pixmap to be displayed while the full image loads."""
        if pixmap and not pixmap.isNull():
            self.set_image(pixmap.toImage(), {})
        else:
            self.set_image(None, {})

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
        self.update()
        self.zoomChanged.emit(self._zoom_factor)

    def reset_zoom(self) -> None:
        """Return the zoom factor to ``1.0`` (fit to window)."""
        self._zoom_factor = 1.0
        self._pan_offset = QPointF(0, 0)
        self.update()
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

    def initializeGL(self) -> None:
        """Setup the OpenGL resources."""
        self._gl_funcs = QOpenGLFunctions_3_3_Core()
        self._gl_funcs.initializeOpenGLFunctions()

        self._shader_program = QOpenGLShaderProgram()
        self._shader_program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX_SHADER)
        self._shader_program.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAGMENT_SHADER)
        self._shader_program.link()

        self._vao = QOpenGLVertexArrayObject()
        self._vao.create()
        self._vao.bind()

        vertices = np.array([
             1.0,  1.0, 0.0, 1.0, 1.0,
             1.0, -1.0, 0.0, 1.0, 0.0,
            -1.0, -1.0, 0.0, 0.0, 0.0,
            -1.0,  1.0, 0.0, 0.0, 1.0
        ], dtype=np.float32)

        indices = np.array([0, 1, 3, 1, 2, 3], dtype=np.uint32)

        self._vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._vbo.create()
        self._vbo.bind()
        self._vbo.allocate(vertices.tobytes(), vertices.nbytes)

        self._gl_funcs.glEnableVertexAttribArray(0)
        self._gl_funcs.glVertexAttribPointer(0, 3, self._gl_funcs.GL_FLOAT, self._gl_funcs.GL_FALSE, 5 * 4, 0)
        self._gl_funcs.glEnableVertexAttribArray(1)
        self._gl_funcs.glVertexAttribPointer(1, 2, self._gl_funcs.GL_FLOAT, self._gl_funcs.GL_FALSE, 5 * 4, 12)

        self._ebo = QOpenGLBuffer(QOpenGLBuffer.IndexBuffer)
        self._ebo.create()
        self._ebo.bind()
        self._ebo.allocate(indices.tobytes(), indices.nbytes)

        self._vao.release()

    def paintGL(self) -> None:
        """Render the image with adjustments."""
        surface_color = QColor(viewer_surface_color(self))
        if surface_color.isValid():
            r, g, b, _ = surface_color.getRgbF()
            self._gl_funcs.glClearColor(r, g, b, 1.0)
        else:
            self._gl_funcs.glClearColor(0.0, 0.0, 0.0, 1.0)
        self._gl_funcs.glClear(self._gl_funcs.GL_COLOR_BUFFER_BIT)

        if not self._texture:
            return

        self._shader_program.bind()
        self._vao.bind()
        self._texture.bind()

        self._shader_program.setUniformValue("u_zoom", self._zoom_factor)
        self._shader_program.setUniformValue("u_pan", self._pan_offset)
        self._shader_program.setUniformValue("is_placeholder", False)

        for key in ["Brilliance", "Exposure", "Highlights", "Shadows", "Brightness", "Contrast", "BlackPoint"]:
            self._shader_program.setUniformValue(key, self._adjustments.get(key, 0.0))

        for key in ["Saturation", "Vibrance", "Cast", "Color_Gain_R", "Color_Gain_G", "Color_Gain_B"]:
            self._shader_program.setUniformValue(key, self._adjustments.get(key, 1.0 if "Gain" in key else 0.0))

        self._gl_funcs.glDrawElements(self._gl_funcs.GL_TRIANGLES, 6, self._gl_funcs.GL_UNSIGNED_INT, None)

        self._texture.release()
        self._vao.release()
        self._shader_program.release()

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
            self.update()
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
