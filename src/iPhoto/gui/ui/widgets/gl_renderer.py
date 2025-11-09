# -*- coding: utf-8 -*-
"""OpenGL renderer used by :class:`GLImageViewer`.

This module isolates all raw OpenGL calls so the widget itself can focus on
state orchestration and Qt event handling.  The renderer loads the GLSL shader
pair, owns the GPU resources (VAO, shader program, texture) and exposes a small
API tailored to the viewer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Optional

import numpy as np
from PySide6.QtCore import QObject, QPointF
from PySide6.QtGui import QImage
from PySide6.QtOpenGL import (
    QOpenGLFunctions_3_3_Core,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLVertexArrayObject,
)
from OpenGL import GL as gl

_LOGGER = logging.getLogger(__name__)


def _load_shader_source(filename: str) -> str:
    """Return the GLSL source stored alongside this module."""

    shader_path = Path(__file__).resolve().with_name(filename)
    try:
        return shader_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to load shader '{filename}': {exc}") from exc


class GLRenderer:
    """Encapsulates the OpenGL drawing routine for the viewer texture."""

    def __init__(
        self,
        gl_funcs: QOpenGLFunctions_3_3_Core,
        *,
        parent: Optional[QObject] = None,
    ) -> None:
        self._gl_funcs = gl_funcs
        self._parent = parent
        self._program: Optional[QOpenGLShaderProgram] = None
        self._dummy_vao: Optional[QOpenGLVertexArrayObject] = None
        self._uniform_locations: dict[str, int] = {}
        self._texture_id: int = 0
        self._texture_width: int = 0
        self._texture_height: int = 0

    # ------------------------------------------------------------------
    # Resource management
    # ------------------------------------------------------------------
    def initialize_resources(self) -> None:
        """Compile the shader program and set up immutable GL state."""

        self.destroy_resources()

        program = QOpenGLShaderProgram(self._parent)
        vert_source = _load_shader_source("gl_image_viewer.vert")
        frag_source = _load_shader_source("gl_image_viewer.frag")
        if not program.addShaderFromSourceCode(QOpenGLShader.Vertex, vert_source):
            message = program.log()
            _LOGGER.error("Vertex shader compilation failed: %s", message)
            raise RuntimeError("Unable to compile vertex shader")
        if not program.addShaderFromSourceCode(QOpenGLShader.Fragment, frag_source):
            message = program.log()
            _LOGGER.error("Fragment shader compilation failed: %s", message)
            raise RuntimeError("Unable to compile fragment shader")
        if not program.link():
            message = program.log()
            _LOGGER.error("Shader program link failed: %s", message)
            raise RuntimeError("Unable to link shader program")

        self._program = program

        vao = QOpenGLVertexArrayObject(self._parent)
        vao.create()
        self._dummy_vao = vao if vao.isCreated() else None

        gf = self._gl_funcs
        gf.glDisable(gl.GL_DEPTH_TEST)
        gf.glDisable(gl.GL_CULL_FACE)
        gf.glDisable(gl.GL_BLEND)

        program.bind()
        try:
            for name in (
                "uTex",
                "uBrilliance",
                "uExposure",
                "uHighlights",
                "uShadows",
                "uBrightness",
                "uContrast",
                "uBlackPoint",
                "uSaturation",
                "uVibrance",
                "uColorCast",
                "uGain",
                "uBWParams",
                "uBWEnabled",
                "uTime",
                "uViewSize",
                "uTexSize",
                "uScale",
                "uPan",
            ):
                self._uniform_locations[name] = program.uniformLocation(name)
        finally:
            program.release()

    def destroy_resources(self) -> None:
        """Release the shader program, VAO and resident texture."""

        self.delete_texture()
        if self._dummy_vao is not None:
            self._dummy_vao.destroy()
            self._dummy_vao = None
        if self._program is not None:
            self._program.removeAllShaders()
            self._program = None
        self._uniform_locations.clear()

    # ------------------------------------------------------------------
    # Texture management
    # ------------------------------------------------------------------
    def upload_texture(self, image: QImage) -> tuple[int, int, int]:
        """Upload *image* to the GPU and return ``(id, width, height)``."""

        if image.isNull():
            raise ValueError("Cannot upload a null QImage")

        # Convert to a tightly packed RGBA8888 surface, which matches the shader
        # expectations and keeps the upload logic uniform for all callers.
        qimage = image.convertToFormat(QImage.Format.Format_RGBA8888)
        width, height = qimage.width(), qimage.height()
        buffer = qimage.constBits()
        byte_count = qimage.sizeInBytes()
        if hasattr(buffer, "setsize"):
            buffer.setsize(byte_count)
        else:
            buffer = buffer[:byte_count]

        if self._texture_id:
            gl.glDeleteTextures([int(self._texture_id)])
            self._texture_id = 0

        tex_id = gl.glGenTextures(1)
        if isinstance(tex_id, (tuple, list)):
            tex_id = tex_id[0]
        self._texture_id = int(tex_id)
        self._texture_width = int(width)
        self._texture_height = int(height)

        gl.glBindTexture(gl.GL_TEXTURE_2D, self._texture_id)
        gl.glTexImage2D(
            gl.GL_TEXTURE_2D,
            0,
            gl.GL_RGBA8,
            width,
            height,
            0,
            gl.GL_RGBA,
            gl.GL_UNSIGNED_BYTE,
            None,
        )
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        row_length = qimage.bytesPerLine() // 4
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, row_length)
        gl.glTexSubImage2D(
            gl.GL_TEXTURE_2D,
            0,
            0,
            0,
            width,
            height,
            gl.GL_RGBA,
            gl.GL_UNSIGNED_BYTE,
            buffer,
        )
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)

        error = gl.glGetError()
        if error != gl.GL_NO_ERROR:
            _LOGGER.warning("OpenGL error after texture upload: 0x%04X", int(error))

        return self._texture_id, self._texture_width, self._texture_height

    def delete_texture(self) -> None:
        """Delete the currently bound texture, if any."""

        if not self._texture_id:
            return
        gl.glDeleteTextures(1, np.array([int(self._texture_id)], dtype=np.uint32))
        self._texture_id = 0
        self._texture_width = 0
        self._texture_height = 0

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render(
        self,
        *,
        view_width: float,
        view_height: float,
        scale: float,
        pan: QPointF,
        adjustments: Mapping[str, float],
        time_value: float | None = None,
    ) -> None:
        """Draw the textured triangle covering the current viewport."""

        if self._program is None:
            raise RuntimeError("Renderer has not been initialised")
        if self._texture_id == 0:
            return
        if scale <= 0.0:
            return

        gf = self._gl_funcs
        if not self._program.bind():
            _LOGGER.error("Failed to bind shader program: %s", self._program.log())
            return

        try:
            if self._dummy_vao is not None:
                self._dummy_vao.bind()

            gf.glActiveTexture(gl.GL_TEXTURE0)
            gf.glBindTexture(gl.GL_TEXTURE_2D, int(self._texture_id))
            self._set_uniform1i("uTex", 0)

            def adjustment_value(key: str, default: float = 0.0) -> float:
                return float(adjustments.get(key, default))

            self._set_uniform1f("uBrilliance", adjustment_value("Brilliance"))
            self._set_uniform1f("uExposure", adjustment_value("Exposure"))
            self._set_uniform1f("uHighlights", adjustment_value("Highlights"))
            self._set_uniform1f("uShadows", adjustment_value("Shadows"))
            self._set_uniform1f("uBrightness", adjustment_value("Brightness"))
            self._set_uniform1f("uContrast", adjustment_value("Contrast"))
            self._set_uniform1f("uBlackPoint", adjustment_value("BlackPoint"))
            self._set_uniform1f("uSaturation", adjustment_value("Saturation"))
            self._set_uniform1f("uVibrance", adjustment_value("Vibrance"))
            self._set_uniform1f("uColorCast", adjustment_value("Cast"))
            self._set_uniform3f(
                "uGain",
                float(adjustments.get("Color_Gain_R", 1.0)),
                float(adjustments.get("Color_Gain_G", 1.0)),
                float(adjustments.get("Color_Gain_B", 1.0)),
            )
            self._set_uniform4f(
                "uBWParams",
                adjustment_value("BWIntensity"),
                adjustment_value("BWNeutrals"),
                adjustment_value("BWTone"),
                adjustment_value("BWGrain"),
            )
            bw_enabled_value = adjustments.get("BW_Enabled", adjustments.get("BWEnabled", 0.0))
            # GLSL represents boolean uniforms as integers, therefore ``glUniform1i``
            # is used to communicate the toggle state without introducing another
            # helper that mirrors the existing ``_set_uniform1i`` wrapper.
            self._set_uniform1i("uBWEnabled", 1 if bool(bw_enabled_value) else 0)
            if time_value is not None:
                self._set_uniform1f("uTime", time_value)

            self._set_uniform1f("uScale", max(scale, 1e-6))
            self._set_uniform2f("uViewSize", max(view_width, 1.0), max(view_height, 1.0))
            self._set_uniform2f(
                "uTexSize",
                float(max(1, self._texture_width)),
                float(max(1, self._texture_height)),
            )
            self._set_uniform2f("uPan", float(pan.x()), float(pan.y()))

            gf.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        finally:
            if self._dummy_vao is not None:
                self._dummy_vao.release()
            self._program.release()

        error = gf.glGetError()
        if error != gl.GL_NO_ERROR:
            _LOGGER.warning("OpenGL error after draw: 0x%04X", int(error))

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def has_texture(self) -> bool:
        """Return ``True`` if a GPU texture is currently resident."""

        return self._texture_id != 0

    def texture_size(self) -> tuple[int, int]:
        """Return the uploaded texture dimensions as ``(width, height)``."""

        return self._texture_width, self._texture_height

    # ------------------------------------------------------------------
    # Uniform helpers
    # ------------------------------------------------------------------
    def _set_uniform1i(self, name: str, value: int) -> None:
        location = self._uniform_locations.get(name, -1)
        if location != -1:
            self._gl_funcs.glUniform1i(location, int(value))

    def _set_uniform1f(self, name: str, value: float) -> None:
        location = self._uniform_locations.get(name, -1)
        if location != -1:
            self._gl_funcs.glUniform1f(location, float(value))

    def _set_uniform2f(self, name: str, x: float, y: float) -> None:
        location = self._uniform_locations.get(name, -1)
        if location != -1:
            self._gl_funcs.glUniform2f(location, float(x), float(y))

    def _set_uniform3f(self, name: str, x: float, y: float, z: float) -> None:
        location = self._uniform_locations.get(name, -1)
        if location != -1:
            self._gl_funcs.glUniform3f(location, float(x), float(y), float(z))

    def _set_uniform4f(self, name: str, x: float, y: float, z: float, w: float) -> None:
        location = self._uniform_locations.get(name, -1)
        if location != -1:
            self._gl_funcs.glUniform4f(location, float(x), float(y), float(z), float(w))
