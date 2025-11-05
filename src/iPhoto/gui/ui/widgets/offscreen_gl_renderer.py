"""Offscreen OpenGL renderer for applying GPU-accelerated image adjustments."""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QSize
from PySide6.QtGui import (
    QColor,
    QImage,
    QOffscreenSurface,
    QOpenGLContext,
)
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFramebufferObject,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVersionProfile,
    QOpenGLVertexArrayObject,
)

from ..palette import viewer_surface_color
from .shaders import FRAGMENT_SHADER, VERTEX_SHADER


class OffscreenGLRenderer:
    """A non-widget renderer that uses an offscreen surface for GL operations."""

    def __init__(self) -> None:
        self._context = QOpenGLContext()
        self._surface = QOffscreenSurface()
        self._surface.create()
        self._context.makeCurrent(self._surface)

        profile = QOpenGLVersionProfile()
        profile.setVersion(3, 3)
        profile.setProfile(QOpenGLVersionProfile.CoreProfile)
        self._gl_funcs = self._context.versionFunctions(profile)
        self._gl_funcs.initializeOpenGLFunctions()

        self._shader_program = QOpenGLShaderProgram()
        self._shader_program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX_SHADER)
        self._shader_program.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAGMENT_SHADER)
        self._shader_program.link()

        self._vao = QOpenGLVertexArrayObject()
        self._vao.create()
        self._vao.bind()

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
        self._context.doneCurrent()

    def create_texture(self, image: QImage) -> QOpenGLTexture:
        """Create a new texture from a QImage."""
        self._context.makeCurrent(self._surface)
        texture = QOpenGLTexture(image.mirrored())
        self._context.doneCurrent()
        return texture

    def render(
        self,
        size: QSize,
        texture: QOpenGLTexture,
        adjustments: dict,
        zoom: float,
        pan: QPointF,
    ) -> QImage:
        """Render the image with adjustments to an FBO and return a QImage."""
        self._context.makeCurrent(self._surface)

        fbo = QOpenGLFramebufferObject(size)
        fbo.bind()

        self._gl_funcs.glViewport(0, 0, size.width(), size.height())

        surface_color = QColor(viewer_surface_color())
        if surface_color.isValid():
            r, g, b, _ = surface_color.getRgbF()
            self._gl_funcs.glClearColor(r, g, b, 1.0)
        else:
            self._gl_funcs.glClearColor(0.0, 0.0, 0.0, 1.0)
        self._gl_funcs.glClear(self._gl_funcs.GL_COLOR_BUFFER_BIT)

        self._shader_program.bind()
        self._vao.bind()
        texture.bind()

        self._shader_program.setUniformValue("u_zoom", zoom)
        self._shader_program.setUniformValue("u_pan", pan)
        self._shader_program.setUniformValue("is_placeholder", False)

        for key in ["Brilliance", "Exposure", "Highlights", "Shadows", "Brightness", "Contrast", "BlackPoint"]:
            self._shader_program.setUniformValue(key, adjustments.get(key, 0.0))

        for key in ["Saturation", "Vibrance", "Cast", "Color_Gain_R", "Color_Gain_G", "Color_Gain_B"]:
            self._shader_program.setUniformValue(key, adjustments.get(key, 1.0 if "Gain" in key else 0.0))

        self._gl_funcs.glDrawElements(self._gl_funcs.GL_TRIANGLES, 6, self._gl_funcs.GL_UNSIGNED_INT, None)

        image = fbo.toImage()

        fbo.release()
        texture.release()
        self._vao.release()
        self._shader_program.release()

        self._context.doneCurrent()
        return image

    def shutdown(self) -> None:
        """Clean up all OpenGL resources."""
        self._context.makeCurrent(self._surface)
        self._vbo.destroy()
        self._ebo.destroy()
        self._vao.destroy()
        self._shader_program.release()
        self._context.doneCurrent()
