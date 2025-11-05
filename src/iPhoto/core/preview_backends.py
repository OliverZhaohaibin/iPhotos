"""Hardware aware preview backends for the edit pipeline."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, TYPE_CHECKING, cast

from array import array
import ctypes

from PySide6.QtGui import QImage

from .image_filters import apply_adjustments

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from PySide6.QtGui import QOffscreenSurface
    from PySide6.QtGui import QOpenGLContext
    from PySide6.QtOpenGL import (
        QOpenGLBuffer,
        QOpenGLFramebufferObject,
        QOpenGLShaderProgram,
    )

_LOGGER = logging.getLogger(__name__)


class PreviewSession(ABC):
    """Represents a backend specific rendering context.

    Sub-classes encapsulate any state that needs to live between individual
    preview renders.  For the CPU fallback this simply wraps the immutable base
    image, while hardware accelerated variants could retain GPU textures or
    frame-buffer identifiers.  The interface is intentionally tiny so each
    backend can expose only what it needs without leaking implementation
    details into the controller layer.
    """

    @abstractmethod
    def dispose(self) -> None:
        """Release resources associated with the session."""


class PreviewBackend(ABC):
    """Abstract preview backend selecting the optimal rendering strategy."""

    tier_name: str = "unknown"
    """Human readable tier label (e.g. ``"CUDA"`` or ``"CPU"``)."""

    supports_realtime: bool = False
    """Whether the backend can render fast enough to run on the UI thread."""

    @abstractmethod
    def create_session(self, image: QImage) -> PreviewSession:
        """Create a rendering session for *image*.

        Each backend is free to convert the image into whatever representation it
        requires.  The controller keeps the returned session alive for as long as
        the asset remains in the edit view.
        """

    @abstractmethod
    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:
        """Apply *adjustments* and return the preview image."""

    def dispose_session(self, session: PreviewSession) -> None:
        """Release resources owned by *session*.

        Backends override this hook when they allocate handles that must be
        explicitly freed.  The default implementation delegates to the session so
        simple wrappers like the CPU fallback do not need any custom logic.
        """

        session.dispose()


@dataclass
class _CpuPreviewSession(PreviewSession):
    """Store the original image for the CPU fallback backend."""

    image: QImage

    def dispose(self) -> None:  # pragma: no cover - nothing to free
        """Release held resources (no-op for pure CPU sessions)."""

        # No explicit resource management is required for the CPU fallback.  The
        # controller simply drops the reference to the session, allowing Python's
        # garbage collector to reclaim the implicit ``QImage`` copy naturally.
        return


class _CpuPreviewBackend(PreviewBackend):
    """CPU implementation using the existing tone-mapping helpers."""

    tier_name = "CPU"
    supports_realtime = False

    def create_session(self, image: QImage) -> PreviewSession:
        return _CpuPreviewSession(image)

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:
        assert isinstance(session, _CpuPreviewSession)
        return apply_adjustments(session.image, adjustments)


class _CudaPreviewBackend(PreviewBackend):
    """Placeholder for a future CUDA accelerated implementation."""

    tier_name = "CUDA"
    supports_realtime = True

    def __init__(self) -> None:
        raise RuntimeError("CUDA backend is not implemented in this build")

    @classmethod
    def is_available(cls) -> bool:
        """Return ``True`` when the runtime provides the required CUDA stack."""

        try:
            import cupy  # type: ignore  # noqa: F401
        except ImportError:
            return False
        _LOGGER.info("CUDA runtime detected but backend is not yet implemented; skipping")
        return False

    def create_session(self, image: QImage) -> PreviewSession:  # pragma: no cover - not reachable
        raise NotImplementedError

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:  # pragma: no cover - not reachable
        raise NotImplementedError


class _OpenGlPreviewBackend(PreviewBackend):
    """OpenGL based preview backend using GLSL shaders for tone mapping."""

    tier_name = "OpenGL"
    supports_realtime = True

    def __init__(self) -> None:
        # Import OpenGL heavy modules lazily so environments without an OpenGL
        # stack (for example headless CI) can still import this module without
        # immediately failing.  ``is_available`` performs the necessary feature
        # probing before the backend is constructed, so any ImportError raised
        # here indicates a configuration drift between the probe and the
        # initialiser.  Surfacing the error keeps the log output actionable.
        from PySide6.QtGui import QOffscreenSurface
        from PySide6.QtGui import QOpenGLContext, QSurfaceFormat
        from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLVersionProfile

        super().__init__()

        self._context: QOpenGLContext = QOpenGLContext()
        format_hint = QSurfaceFormat()
        format_hint.setVersion(3, 3)
        format_hint.setProfile(QSurfaceFormat.CoreProfile)
        format_hint.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        self._context.setFormat(format_hint)
        if not self._context.create():
            raise RuntimeError("Failed to create OpenGL context")

        self._surface: QOffscreenSurface = QOffscreenSurface()
        self._surface.setFormat(self._context.format())
        self._surface.create()
        if not self._surface.isValid():
            raise RuntimeError("OpenGL offscreen surface is invalid")

        if not self._context.makeCurrent(self._surface):
            raise RuntimeError("Failed to make OpenGL context current")

        profile = QOpenGLVersionProfile()
        profile.setVersion(3, 3)
        functions = self._context.versionFunctions(profile)
        if not functions:
            raise RuntimeError("Failed to retrieve OpenGL 3.3 functions")
        functions.initializeOpenGLFunctions()
        self._gl = functions

        # Compile and link the shader program once.  The uniforms mirror the
        # tone-mapping helper in :mod:`iPhoto.core.image_filters` so both
        # backends stay in sync.  Compiling upfront amortises the cost across
        # all sessions, ensuring interactive slider tweaks remain snappy.
        self._program: QOpenGLShaderProgram = QOpenGLShaderProgram()
        vertex_shader = QOpenGLShader(QOpenGLShader.ShaderTypeBit.Vertex)
        if not vertex_shader.compileSourceCode(self._vertex_shader_source()):
            message = vertex_shader.log() or "unknown vertex shader error"
            raise RuntimeError(f"Failed to compile OpenGL vertex shader: {message}")
        fragment_shader = QOpenGLShader(QOpenGLShader.ShaderTypeBit.Fragment)
        if not fragment_shader.compileSourceCode(self._fragment_shader_source()):
            message = fragment_shader.log() or "unknown fragment shader error"
            raise RuntimeError(f"Failed to compile OpenGL fragment shader: {message}")
        self._program.addShader(vertex_shader)
        self._program.addShader(fragment_shader)
        if not self._program.link():
            message = self._program.log() or "unknown shader link error"
            raise RuntimeError(f"Failed to link OpenGL shader program: {message}")

        # Cache attribute/uniform locations to avoid repeated string lookups
        # during each render call.  The attribute layout is a simple quad that
        # covers the entire normalised device coordinate range.
        self._position_location = self._program.attributeLocation("a_position")
        self._texcoord_location = self._program.attributeLocation("a_texcoord")
        self._uniform_source = self._program.uniformLocation("uSourceTexture")
        self._uniform_exposure = self._program.uniformLocation("uExposureTerm")
        self._uniform_brightness = self._program.uniformLocation("uBrightnessTerm")
        self._uniform_brilliance = self._program.uniformLocation("uBrillianceStrength")
        self._uniform_highlights = self._program.uniformLocation("uHighlights")
        self._uniform_shadows = self._program.uniformLocation("uShadows")
        self._uniform_contrast = self._program.uniformLocation("uContrastFactor")
        self._uniform_black_point = self._program.uniformLocation("uBlackPoint")

        # Prepare the vertex buffer containing a full screen triangle strip.
        vertices = array(
            "f",
            [
                -1.0,
                -1.0,
                0.0,
                1.0,
                1.0,
                -1.0,
                1.0,
                1.0,
                -1.0,
                1.0,
                0.0,
                0.0,
                1.0,
                1.0,
                1.0,
                0.0,
            ],
        )
        self._vertex_buffer: QOpenGLBuffer = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        if not self._vertex_buffer.create():
            raise RuntimeError("Failed to create OpenGL vertex buffer")
        if not self._vertex_buffer.bind():
            raise RuntimeError("Failed to bind OpenGL vertex buffer")
        raw_vertices = vertices.tobytes()
        self._vertex_buffer.allocate(raw_vertices, len(raw_vertices))
        self._vertex_buffer.release()

        self._context.doneCurrent()

    @staticmethod
    def _vertex_shader_source() -> str:
        """Return the GLSL source code for the fullscreen quad vertex shader."""
        return """
#version 330 core
layout (location = 0) in vec2 a_position;
layout (location = 1) in vec2 a_texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(a_position, 0.0, 1.0);
    v_texcoord = a_texcoord;
}
"""

    @staticmethod
    def _fragment_shader_source() -> str:
        """Return the GLSL source code mirroring ``_apply_channel_adjustments``."""
        return """
#version 330 core
out vec4 FragColor;
in vec2 v_texcoord;
uniform sampler2D uSourceTexture;
uniform float uExposureTerm;
uniform float uBrightnessTerm;
uniform float uBrillianceStrength;
uniform float uHighlights;
uniform float uShadows;
uniform float uContrastFactor;
uniform float uBlackPoint;
float apply_channel(float value) {
    float adjusted = value + uExposureTerm + uBrightnessTerm;
    float mid_distance = value - 0.5;
    float spread = (mid_distance * 2.0);
    adjusted += uBrillianceStrength * (1.0 - (spread * spread));
    if (adjusted > 0.65) {
        float ratio = (adjusted - 0.65) / 0.35;
        adjusted += uHighlights * ratio;
    } else if (adjusted < 0.35) {
        float ratio = (0.35 - adjusted) / 0.35;
        adjusted += uShadows * ratio;
    }
    adjusted = (adjusted - 0.5) * uContrastFactor + 0.5;
    if (uBlackPoint > 0.0) {
        adjusted -= uBlackPoint * (1.0 - adjusted);
    } else if (uBlackPoint < 0.0) {
        adjusted -= uBlackPoint * adjusted;
    }
    return clamp(adjusted, 0.0, 1.0);
}
void main() {
    vec4 tex_color = texture(uSourceTexture, v_texcoord);
    tex_color.r = apply_channel(tex_color.r);
    tex_color.g = apply_channel(tex_color.g);
    tex_color.b = apply_channel(tex_color.b);
    FragColor = tex_color;
}
"""

    @classmethod
    def is_available(cls) -> bool:
        """Return ``True`` if an OpenGL rendering context is available."""

        try:
            from PySide6.QtGui import QOffscreenSurface
            from PySide6.QtGui import QOpenGLContext, QSurfaceFormat
        except Exception:
            return False

        try:
            context = QOpenGLContext()
            format_hint = QSurfaceFormat()
            format_hint.setVersion(3, 3)
            format_hint.setProfile(QSurfaceFormat.CoreProfile)
            format_hint.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            context.setFormat(format_hint)
            if not context.create():
                return False

            surface = QOffscreenSurface()
            surface.setFormat(context.format())
            surface.create()
            if not surface.isValid():
                return False

            if not context.makeCurrent(surface):
                return False

            functions = context.functions()
            texture_ids = (ctypes.c_uint * 1)()
            functions.glGenTextures(1, texture_ids)
            texture_id = int(texture_ids[0])
            if texture_id == 0:
                return False
            functions.glDeleteTextures(1, texture_ids)
        except Exception:
            return False
        finally:
            try:
                context.doneCurrent()
            except Exception:  # pragma: no cover - defensive
                pass

        return True

    def _make_current(self) -> bool:
        """Attempt to activate the shared OpenGL context."""

        try:
            return self._context.makeCurrent(self._surface)
        except Exception:
            return False

    def create_session(self, image: QImage) -> PreviewSession:
        from PySide6.QtGui import QImage as QtImage
        from PySide6.QtOpenGL import QOpenGLFramebufferObject

        if image.isNull():
            # Return a lightweight placeholder session so callers can proceed
            # without handling a special case.  Rendering an empty session
            # results in a null image which mirrors the CPU backend's
            # behaviour when asked to process an invalid ``QImage``.
            return _OpenGlPreviewSession(0, 0, 0, None)

        if not self._make_current():
            raise RuntimeError("Failed to activate OpenGL context for session creation")

        converted = image.convertToFormat(QtImage.Format.Format_RGBA8888)
        width = converted.width()
        height = converted.height()

        # Upload pixels using a Qt-managed FBO + QPainter.  This avoids
        # low-level glTexImage2D uploads and common cross-context/driver
        # problems with raw pointer uploads.  The upload returns a texture
        # id owned by Qt; keep a reference to the FBO so Qt doesn't free it
        # while we still sample from the texture.
        texture_id, upload_fbo = self._upload_texture(0, converted)

        # Destination framebuffer (separate from the upload FBO) to render
        # into. The upload_fbo keeps ownership of the source texture.
        framebuffer = QOpenGLFramebufferObject(width, height)

        self._context.doneCurrent()

        return _OpenGlPreviewSession(width, height, texture_id, framebuffer, texture_owned=False)

    def _generate_texture(self) -> int:
        """Create and return a new OpenGL texture identifier."""

        texture_ids = (ctypes.c_uint * 1)()
        self._gl.glGenTextures(1, texture_ids)
        return int(texture_ids[0])

    def _upload_texture(self, texture_id: int, image: QImage) -> tuple[int, "QOpenGLFramebufferObject"]:
        """Upload *image* into a Qt-managed FBO using QPainter and return
        the texture id and the FBO instance.

        This approach delegates pixel conversion/upload to Qt which is more
        robust across platforms and avoids manual glTexImage2D calls.
        """

        # Ensure the GL context is current for FBO creation and QPainter use
        if not self._make_current():
            raise RuntimeError("OpenGL context must be current for texture upload")

        from PySide6.QtOpenGL import QOpenGLFramebufferObject
        from PySide6.QtGui import QPainter

        # Create a temporary FBO and draw the QImage into it using QPainter.
        upload_fbo = QOpenGLFramebufferObject(image.width(), image.height())
        painter = QPainter(upload_fbo)
        try:
            # Draw the image at origin; QPainter handles format/row-order
            # differences reliably across platforms.
            painter.drawImage(0, 0, image)
        finally:
            painter.end()

        tex = int(upload_fbo.texture())

        # Keep a reference so Qt doesn't free the underlying texture while
        # we still reference it. Store on the backend to allow cleanup later.
        if not hasattr(self, '_temp_upload_fbos'):
            self._temp_upload_fbos = []
        self._temp_upload_fbos.append(upload_fbo)

        return tex, upload_fbo

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:
        from PySide6.QtGui import QImage as QtImage
        from typing import cast

        gl_session = cast(_OpenGlPreviewSession, session)
        if gl_session.width == 0 or gl_session.height == 0:
            return QImage()

        if not self._make_current():
            raise RuntimeError("Failed to activate OpenGL context for preview rendering")

        gl = self._gl
        framebuffer = gl_session.framebuffer
        if framebuffer is None:
            self._context.doneCurrent()
            return QImage()

        if not framebuffer.bind():
            self._context.doneCurrent()
            raise RuntimeError("Failed to bind OpenGL framebuffer")

        gl.glViewport(0, 0, gl_session.width, gl_session.height)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)

        program = self._program
        if not program.bind():
            framebuffer.release()
            self._context.doneCurrent()
            raise RuntimeError("Failed to bind OpenGL shader program")

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, gl_session.texture_id)
        program.setUniformValue(self._uniform_source, 0)

        exposure_term = float(adjustments.get("Exposure", 0.0)) * 1.5
        brightness_term = float(adjustments.get("Brightness", 0.0)) * 0.75
        brilliance_strength = float(adjustments.get("Brilliance", 0.0)) * 0.6
        highlights = float(adjustments.get("Highlights", 0.0))
        shadows = float(adjustments.get("Shadows", 0.0))
        contrast_factor = 1.0 + float(adjustments.get("Contrast", 0.0))
        black_point = float(adjustments.get("BlackPoint", 0.0))

        program.setUniformValue(self._uniform_exposure, exposure_term)
        program.setUniformValue(self._uniform_brightness, brightness_term)
        program.setUniformValue(self._uniform_brilliance, brilliance_strength)
        program.setUniformValue(self._uniform_highlights, highlights)
        program.setUniformValue(self._uniform_shadows, shadows)
        program.setUniformValue(self._uniform_contrast, contrast_factor)
        program.setUniformValue(self._uniform_black_point, black_point)

        if not self._vertex_buffer.bind():
            program.release()
            framebuffer.release()
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            self._context.doneCurrent()
            raise RuntimeError("Failed to bind OpenGL vertex buffer for rendering")

        stride = 4 * 4  # 4 floats (vec2 position + vec2 texcoord)
        program.enableAttributeArray(self._position_location)
        program.setAttributeBuffer(self._position_location, gl.GL_FLOAT, 0, 2, stride)
        program.enableAttributeArray(self._texcoord_location)
        program.setAttributeBuffer(self._texcoord_location, gl.GL_FLOAT, 2 * 4, 2, stride)

        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)

        program.disableAttributeArray(self._position_location)
        program.disableAttributeArray(self._texcoord_location)
        self._vertex_buffer.release()
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        program.release()

        image = framebuffer.toImage(True).convertToFormat(QtImage.Format.Format_ARGB32)
        framebuffer.release()

        self._context.doneCurrent()

        return image

    def dispose_session(self, session: PreviewSession) -> None:
        gl_session = cast(_OpenGlPreviewSession, session)

        if gl_session.texture_id == 0 and gl_session.framebuffer is None:
            return

        if self._make_current():
            # Only delete textures that we explicitly created via glGenTextures
            if getattr(gl_session, 'texture_owned', True) and gl_session.texture_id != 0:
                try:
                    texture_ids = (ctypes.c_uint * 1)(gl_session.texture_id)
                    self._gl.glDeleteTextures(1, texture_ids)
                except Exception:
                    pass
                gl_session.texture_id = 0
            framebuffer = gl_session.framebuffer
            if framebuffer is not None:
                # ``QOpenGLFramebufferObject`` releases its resources when the
                # Python wrapper is destroyed.  Clearing our reference allows Qt
                # to free the underlying OpenGL object while the context is
                # still current.
                if framebuffer.isBound():
                    framebuffer.release()
                gl_session.framebuffer = None
                del framebuffer
            self._context.doneCurrent()

        gl_session.dispose()


@dataclass
class _OpenGlPreviewSession(PreviewSession):
    """Hold OpenGL resources tied to a single preview image."""

    width: int
    height: int
    texture_id: int
    framebuffer: "QOpenGLFramebufferObject | None"
    # Whether the texture id was created by glGenTextures (True) or comes
    # from a Qt-managed FBO (False). Only owned textures should be explicitly
    # deleted via glDeleteTextures.
    texture_owned: bool = True

    def dispose(self) -> None:  # pragma: no cover - real cleanup happens in backend
        self.framebuffer = None
        self.texture_id = 0


def select_preview_backend() -> PreviewBackend:
    """Return the most capable preview backend available on the system."""

    # CUDA backend has the highest priority.
    if _CudaPreviewBackend.is_available():
        try:
            backend = _CudaPreviewBackend()
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Failed to initialise CUDA backend: %s", exc)
        else:
            _LOGGER.info("Using CUDA preview backend")
            return backend

    # OpenGL is the next best choice when CUDA is not available.
    if _OpenGlPreviewBackend.is_available():
        try:
            backend = _OpenGlPreviewBackend()
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Failed to initialise OpenGL backend: %s", exc)
        else:
            _LOGGER.info("Using OpenGL preview backend")
            return backend

    backend = _CpuPreviewBackend()
    _LOGGER.info("Falling back to CPU preview backend")
    return backend


def fallback_preview_backend(previous: PreviewBackend) -> PreviewBackend:
    """Return a safer backend after *previous* reports a fatal failure."""

    # ``_CudaPreviewBackend`` currently raises during construction but the
    # ``isinstance`` guard keeps the helper forward-compatible for future
    # implementations.  Prefer stepping down one tier at a time so the caller
    # retains hardware acceleration whenever possible.
    if isinstance(previous, _CudaPreviewBackend):  # pragma: no cover - defensive
        if _OpenGlPreviewBackend.is_available():
            try:
                backend = _OpenGlPreviewBackend()
            except Exception:
                pass
            else:
                _LOGGER.info(
                    "Falling back from CUDA preview backend to OpenGL implementation",
                )
                return backend

    if isinstance(previous, _OpenGlPreviewBackend):
        _LOGGER.info("Falling back from OpenGL preview backend to CPU implementation")
        return _CpuPreviewBackend()

    # Any other backend (including the CPU fallback) drops straight to the
    # baseline CPU implementation so callers always receive a usable renderer.
    return _CpuPreviewBackend()


__all__ = [
    "PreviewBackend",
    "PreviewSession",
    "fallback_preview_backend",
    "select_preview_backend",
]
