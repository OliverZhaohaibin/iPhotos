"""Widget that displays a scaled image while preserving aspect ratio."""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QImage,
    QMouseEvent,
    QOpenGLFunctions,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVersionProfile,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

VERTEX_SHADER = """
#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec2 aTexCoord;

out vec2 TexCoord;

uniform float u_zoom;
uniform vec2 u_pan;

void main()
{
    gl_Position = vec4(aPos * u_zoom + vec3(u_pan, 0.0), 1.0);
    TexCoord = aTexCoord;
}
"""

FRAGMENT_SHADER = """
#version 330 core
out vec4 FragColor;

in vec2 TexCoord;

uniform sampler2D ourTexture;
uniform bool is_placeholder;

// Light Adjustments
uniform float Brilliance;
uniform float Exposure;
uniform float Highlights;
uniform float Shadows;
uniform float Brightness;
uniform float Contrast;
uniform float BlackPoint;

// Color Adjustments
uniform float Saturation;
uniform float Vibrance;
uniform float Cast;
uniform float Color_Gain_R;
uniform float Color_Gain_G;
uniform float Color_Gain_B;

// This is a GLSL port of the logic from light_resolver.py and color_resolver.py
vec3 apply_adjustments(vec3 color) {
    // Apply White Balance
    color.r *= Color_Gain_R;
    color.g *= Color_Gain_G;
    color.b *= Color_Gain_B;

    // Exposure
    color *= pow(2.0, Exposure);

    // Brilliance
    float brilliance_val = Brilliance;
    if (brilliance_val > 0.0) {
        color = color * (1.0 - brilliance_val) + (color * color) * brilliance_val;
    } else {
        color = color * (1.0 + brilliance_val) - (color * color) * brilliance_val;
    }

    // Highlights and Shadows
    float highlights_val = Highlights;
    float shadows_val = Shadows;
    float luma = dot(color, vec3(0.2126, 0.7152, 0.0722));
    float shadow_factor = 1.0 - smoothstep(0.0, 0.4, luma);
    float highlight_factor = smoothstep(0.6, 1.0, luma);
    color += shadow_factor * shadows_val;
    color -= highlight_factor * highlights_val;

    // Brightness
    color += Brightness;

    // Contrast
    color = (color - 0.5) * (1.0 + Contrast) + 0.5;

    // BlackPoint
    color = max(color - BlackPoint, 0.0);

    // Saturation
    vec3 grayscale = vec3(dot(color, vec3(0.299, 0.587, 0.114)));
    color = mix(grayscale, color, 1.0 + Saturation);

    // Vibrance
    float vibrance_val = Vibrance;
    float max_color = max(color.r, max(color.g, color.b));
    float min_color = min(color.r, min(color.g, color.b));
    float sat = max_color - min_color;
    color = mix(grayscale, color, 1.0 + vibrance_val * (1.0 - sat));

    return clamp(color, 0.0, 1.0);
}


void main()
{
    vec4 texColor = texture(ourTexture, TexCoord);
    if (is_placeholder) {
        FragColor = texColor;
    } else {
        vec3 color = apply_adjustments(texColor.rgb);
        FragColor = vec4(color, texColor.a);
    }
}
"""

class GLImageViewer(QOpenGLWidget):
    """OpenGL-accelerated viewer that centers, zooms, and scrolls an image."""

    replayRequested = Signal()
    zoomChanged = Signal(float)
    nextItemRequested = Signal()
    prevItemRequested = Signal()
    fullscreenExitRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._image: Optional[QImage] = None
        self._texture: Optional[QOpenGLTexture] = None
        self._placeholder_image: Optional[QImage] = None
        self._placeholder_texture: Optional[QOpenGLTexture] = None
        self._texture_dirty = True
        self._placeholder_dirty = True
        self._shader_program: Optional[QOpenGLShaderProgram] = None
        self._adjustments: dict[str, float] = {}
        self._vao = QOpenGLVertexArrayObject()
        self._vbo = QOpenGLBuffer()
        self._gl_funcs: Optional[QOpenGLFunctions] = None
        self._live_replay_enabled = False

        self._zoom_factor = 1.0
        self._min_zoom = 0.1
        self._max_zoom = 4.0
        self._pan_offset = QPointF(0, 0)
        self._is_panning = False
        self._pan_start_pos = QPointF()
        self._wheel_action = "navigate"

    def set_image(self, image: Optional[QImage]) -> None:
        """Set the image to be displayed."""
        self._image = image
        self._texture_dirty = True
        self.reset_zoom()
        self.update()

    def set_placeholder(self, pixmap: Optional[QPixmap]) -> None:
        """Set a placeholder pixmap to be displayed while the full image loads."""
        if pixmap and not pixmap.isNull():
            self._placeholder_image = pixmap.toImage()
        else:
            self._placeholder_image = None
        self._placeholder_dirty = True
        self.update()

    def set_adjustments(self, adjustments: dict[str, float]) -> None:
        """Set the image adjustments."""
        self._adjustments = adjustments
        self.update()

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

            # Convert pixel delta to normalized device coordinates
            pan_delta_x = 2.0 * delta.x() / self.width()
            pan_delta_y = -2.0 * delta.y() / self.height() # Y is inverted in OpenGL

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

    def initializeGL(self) -> None:
        """Set up the rendering context, load shaders, and allocate resources."""
        profile = QOpenGLVersionProfile()
        profile.setVersion(3, 3)
        profile.setProfile(QOpenGLVersionProfile.CoreProfile)
        self._gl_funcs = self.context().functions()
        self._gl_funcs.initializeOpenGLFunctions()
        self._gl_funcs.glClearColor(0.0, 0.0, 0.0, 1.0)

        self._shader_program = QOpenGLShaderProgram()
        self._shader_program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX_SHADER)
        self._shader_program.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAGMENT_SHADER)
        self._shader_program.link()
        self._shader_program.bind()

        vertices = np.array([
            # positions      # texture coords
             1.0,  1.0, 0.0,  1.0, 1.0, # top right
             1.0, -1.0, 0.0,  1.0, 0.0, # bottom right
            -1.0, -1.0, 0.0,  0.0, 0.0, # bottom left
            -1.0,  1.0, 0.0,  0.0, 1.0  # top left
        ], dtype=np.float32)

        indices = np.array([
            0, 1, 3, # first triangle
            1, 2, 3  # second triangle
        ], dtype=np.uint32)

        self._vao.create()
        self._vao.bind()

        self._vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._vbo.create()
        self._vbo.bind()
        self._vbo.allocate(vertices.tobytes(), vertices.nbytes)

        self._gl_funcs.glEnableVertexAttribArray(0)
        self._gl_funcs.glVertexAttribPointer(0, 3, self._gl_funcs.GL_FLOAT, self._gl_funcs.GL_FALSE, 5 * 4, 0)
        self._gl_funcs.glEnableVertexAttribArray(1)
        self._gl_funcs.glVertexAttribPointer(1, 2, self._gl_funcs.GL_FLOAT, self._gl_funcs.GL_FALSE, 5 * 4, 3 * 4)

        self._ebo = QOpenGLBuffer(QOpenGLBuffer.IndexBuffer)
        self._ebo.create()
        self._ebo.bind()
        self._ebo.allocate(indices.tobytes(), indices.nbytes)

        self._vao.release()
        self._shader_program.release()

    def paintGL(self) -> None:
        """Render the current frame inside the active OpenGL context."""
        if not self._gl_funcs:
            return

        if self._placeholder_dirty:
            if self._placeholder_texture:
                self._placeholder_texture.destroy()
                self._placeholder_texture = None

            if self._placeholder_image and not self._placeholder_image.isNull():
                self._placeholder_texture = QOpenGLTexture(self._placeholder_image.mirrored())

            self._placeholder_dirty = False

        if self._texture_dirty:
            if self._texture:
                self._texture.destroy()
                self._texture = None

            if self._image and not self._image.isNull():
                self._texture = QOpenGLTexture(self._image.mirrored())

            self._texture_dirty = False

        self._gl_funcs.glClear(self._gl_funcs.GL_COLOR_BUFFER_BIT)

        texture_to_render = self._texture if self._texture else self._placeholder_texture
        is_placeholder = not self._texture and self._placeholder_texture

        if not texture_to_render or not self._shader_program:
            return

        self._shader_program.bind()
        self._vao.bind()
        texture_to_render.bind()

        self._shader_program.setUniformValue("u_zoom", self._zoom_factor)
        self._shader_program.setUniformValue("u_pan", self._pan_offset)
        self._shader_program.setUniformValue("is_placeholder", is_placeholder)

        # Set uniforms
        for key in ["Brilliance", "Exposure", "Highlights", "Shadows", "Brightness", "Contrast", "BlackPoint"]:
            self._shader_program.setUniformValue(key, self._adjustments.get(key, 0.0))

        for key in ["Saturation", "Vibrance", "Cast", "Color_Gain_R", "Color_Gain_G", "Color_Gain_B"]:
             self._shader_program.setUniformValue(key, self._adjustments.get(key, 1.0 if "Gain" in key else 0.0))

        self._gl_funcs.glDrawElements(self._gl_funcs.GL_TRIANGLES, 6, self._gl_funcs.GL_UNSIGNED_INT, None)

        texture_to_render.release()
        self._vao.release()
        self._shader_program.release()

    def resizeGL(self, w: int, h: int) -> None:
        """Called when the widget is resized."""
        if not self._gl_funcs:
            return

        iw, ih = 0, 0
        if self._image:
            iw = self._image.width()
            ih = self._image.height()
        elif self._placeholder_texture:
            iw = self._placeholder_texture.width()
            ih = self._placeholder_texture.height()

        if iw > 0 and ih > 0:
            # maintain aspect ratio
            ww = w
            wh = h

            width_ratio = ww / iw
            height_ratio = wh / ih

            scale = min(width_ratio, height_ratio)

            nw = int(iw * scale)
            nh = int(ih * scale)

            nx = int((ww-nw)/2)
            ny = int((wh-nh)/2)

            self._gl_funcs.glViewport(nx, ny, nw, nh)
        else:
             self._gl_funcs.glViewport(0, 0, w, h)
