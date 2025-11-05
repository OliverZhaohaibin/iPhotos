"""Widget that displays a scaled image while preserving aspect ratio."""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QOpenGLShader, QOpenGLShaderProgram, QOpenGLVersionProfile, QPixmap
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLFunctions, QOpenGLTexture, QOpenGLVertexArrayObject
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

VERTEX_SHADER = """
#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec2 aTexCoord;

out vec2 TexCoord;

void main()
{
    gl_Position = vec4(aPos, 1.0);
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

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._image: Optional[QImage] = None
        self._texture: Optional[QOpenGLTexture] = None
        self._placeholder_texture: Optional[QOpenGLTexture] = None
        self._shader_program: Optional[QOpenGLShaderProgram] = None
        self._adjustments: dict[str, float] = {}
        self._vao = QOpenGLVertexArrayObject()
        self._vbo = QOpenGLBuffer()
        self._gl_funcs: Optional[QOpenGLFunctions] = None
        self._live_replay_enabled = False

    def set_image(self, image: Optional[QImage]) -> None:
        """Set the image to be displayed."""
        self.makeCurrent()
        self._image = image
        if self._texture:
            self._texture.destroy()
        if self._image and not self._image.isNull():
             self._texture = QOpenGLTexture(self._image.mirrored())
        else:
            self._texture = None
        self.doneCurrent()
        self.update()

    def set_placeholder(self, pixmap: Optional[QPixmap]) -> None:
        """Set a placeholder pixmap to be displayed while the full image loads."""
        self.makeCurrent()
        if self._placeholder_texture:
            self._placeholder_texture.destroy()
        if pixmap and not pixmap.isNull():
            self._placeholder_texture = QOpenGLTexture(pixmap.toImage().mirrored())
        else:
            self._placeholder_texture = None
        self.doneCurrent()
        self.update()

    def set_adjustments(self, adjustments: dict[str, float]) -> None:
        """Set the image adjustments."""
        self._adjustments = adjustments
        self.update()

    def set_live_replay_enabled(self, enabled: bool) -> None:
        """Allow emitting replay requests when the still frame is shown."""
        self._live_replay_enabled = bool(enabled)

    def mousePressEvent(self, event):
        if self._live_replay_enabled:
            self.replayRequested.emit()
        super().mousePressEvent(event)

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
        self._gl_funcs.glClear(self._gl_funcs.GL_COLOR_BUFFER_BIT)

        texture_to_render = self._texture if self._texture else self._placeholder_texture
        is_placeholder = not self._texture and self._placeholder_texture

        if not texture_to_render or not self._shader_program:
            return

        self._shader_program.bind()
        self._vao.bind()
        texture_to_render.bind()

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
        image_to_size = self._image if self._image else (self._placeholder_texture.image() if self._placeholder_texture else None)

        if image_to_size:
            # maintain aspect ratio
            iw = image_to_size.width()
            ih = image_to_size.height()
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
