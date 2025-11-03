"""Hardware aware preview backends for the edit pipeline."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, TYPE_CHECKING, cast

from array import array
import ctypes
import struct

from PySide6.QtGui import QImage

from .image_filters import apply_adjustments
from .color_resolver import ColorStats, compute_color_statistics

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
    """Store the original image and precomputed statistics for the CPU backend."""

    image: QImage
    color_stats: ColorStats

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
        stats = compute_color_statistics(image) if not image.isNull() else ColorStats()
        return _CpuPreviewSession(image, stats)

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:
        assert isinstance(session, _CpuPreviewSession)
        return apply_adjustments(session.image, adjustments, color_stats=session.color_stats)


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
        from PySide6.QtGui import (
            QOffscreenSurface,
            QOpenGLContext,
            QOpenGLFunctions_4_3_Core,
            QOpenGLVersionFunctionsFactory,
            QSurfaceFormat,
        )
        from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram

        super().__init__()

        self._context: QOpenGLContext | None = None
        self._context_version: tuple[int, int] | None = None
        context_error: Exception | None = None
        for major, minor in ((4, 3), (3, 3)):
            candidate = QOpenGLContext()
            format_hint = QSurfaceFormat()
            format_hint.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            format_hint.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            format_hint.setVersion(major, minor)
            candidate.setFormat(format_hint)
            try:
                if candidate.create():
                    self._context = candidate
                    self._context_version = (major, minor)
                    break
            except Exception as exc:  # pragma: no cover - defensive guard
                context_error = exc
                continue
        if self._context is None:
            message = "Failed to create OpenGL context"
            if context_error is not None:
                message = f"{message}: {context_error}"
            raise RuntimeError(message)

        self._surface: QOffscreenSurface = QOffscreenSurface()
        self._surface.setFormat(self._context.format())
        self._surface.create()
        if not self._surface.isValid():
            raise RuntimeError("OpenGL offscreen surface is invalid")

        if not self._context.makeCurrent(self._surface):
            raise RuntimeError("Failed to make OpenGL context current")

        functions = self._context.functions()
        functions.initializeOpenGLFunctions()
        self._gl = functions
        self._gl43: QOpenGLFunctions_4_3_Core | None = None
        if self._context_version is not None and self._context_version >= (4, 3):
            self._gl43 = QOpenGLVersionFunctionsFactory.get(
                self._context, QOpenGLFunctions_4_3_Core
            )
            if self._gl43 is not None:
                self._gl43.initializeOpenGLFunctions()

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
        self._uniform_saturation = self._program.uniformLocation("uSaturation")
        self._uniform_vibrance = self._program.uniformLocation("uVibrance")
        self._uniform_cast = self._program.uniformLocation("uCast")
        self._uniform_gain = self._program.uniformLocation("uGain")

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

        self._compute_program: QOpenGLShaderProgram | None = None
        self._stats_buffer: QOpenGLBuffer | None = None
        try:
            self._compute_program = self._compile_compute_shader()
            self._stats_buffer = QOpenGLBuffer(QOpenGLBuffer.Type.ShaderStorageBuffer)
            if not self._stats_buffer.create():
                self._stats_buffer = None
        except Exception:
            # Compute shader support is optional; gracefully fall back to CPU
            # statistics on hardware that lacks OpenGL 4.3 features.
            self._compute_program = None
            self._stats_buffer = None

        self._context.doneCurrent()

    @staticmethod
    def _vertex_shader_source() -> str:
        """Return the GLSL source code for the fullscreen quad vertex shader."""

        return (
            "#version 330\n"
            "in vec2 a_position;\n"
            "in vec2 a_texcoord;\n"
            "out vec2 v_texcoord;\n"
            "void main() {\n"
            "    gl_Position = vec4(a_position, 0.0, 1.0);\n"
            "    v_texcoord = a_texcoord;\n"
            "}\n"
        )

    @staticmethod
    def _fragment_shader_source() -> str:
        """Return the GLSL source code mirroring ``_apply_channel_adjustments``."""

        return (
            "#version 330\n"
            "uniform sampler2D uSourceTexture;\n"
            "uniform float uExposureTerm;\n"
            "uniform float uBrightnessTerm;\n"
            "uniform float uBrillianceStrength;\n"
            "uniform float uHighlights;\n"
            "uniform float uShadows;\n"
            "uniform float uContrastFactor;\n"
            "uniform float uBlackPoint;\n"
            "uniform float uSaturation;\n"
            "uniform float uVibrance;\n"
            "uniform float uCast;\n"
            "uniform vec3 uGain;\n"
            "in vec2 v_texcoord;\n"
            "out vec4 FragColor;\n"
            "float clamp01(float value) {\n"
            "    return clamp(value, 0.0, 1.0);\n"
            "}\n"
            "float apply_channel(float value) {\n"
            "    float adjusted = value + uExposureTerm + uBrightnessTerm;\n"
            "    float mid_distance = value - 0.5;\n"
            "    float spread = (mid_distance * 2.0);\n"
            "    adjusted += uBrillianceStrength * (1.0 - (spread * spread));\n"
            "    if (adjusted > 0.65) {\n"
            "        float ratio = (adjusted - 0.65) / 0.35;\n"
            "        adjusted += uHighlights * ratio;\n"
            "    } else if (adjusted < 0.35) {\n"
            "        float ratio = (0.35 - adjusted) / 0.35;\n"
            "        adjusted += uShadows * ratio;\n"
            "    }\n"
            "    adjusted = (adjusted - 0.5) * uContrastFactor + 0.5;\n"
            "    if (uBlackPoint > 0.0) {\n"
            "        adjusted -= uBlackPoint * (1.0 - adjusted);\n"
            "    } else if (uBlackPoint < 0.0) {\n"
            "        adjusted -= uBlackPoint * adjusted;\n"
            "    }\n"
            "    return clamp01(adjusted);\n"
            "}\n"
            "void main() {\n"
            "    vec4 tex_color = texture(uSourceTexture, v_texcoord);\n"
            "    tex_color.r = apply_channel(tex_color.r);\n"
            "    tex_color.g = apply_channel(tex_color.g);\n"
            "    tex_color.b = apply_channel(tex_color.b);\n"
            "    vec3 color = tex_color.rgb * mix(vec3(1.0), uGain, clamp(uCast, 0.0, 1.0));\n"
            "    float luma = dot(color, vec3(0.299, 0.587, 0.114));\n"
            "    vec3 chroma = color - vec3(luma);\n"
            "    float satAmt = 1.0 + uSaturation;\n"
            "    float vibAmt = 1.0 + uVibrance;\n"
            "    float w = 1.0 - clamp(abs(luma - 0.5) * 2.0, 0.0, 1.0);\n"
            "    chroma *= satAmt * mix(1.0, vibAmt, w);\n"
            "    vec3 output_color = clamp(vec3(luma) + chroma, 0.0, 1.0);\n"
            "    FragColor = vec4(output_color, tex_color.a);\n"
            "}\n"
        )

    def _compile_compute_shader(self) -> "QOpenGLShaderProgram | None":
        """Compile the compute shader used to gather Color statistics."""

        if self._gl43 is None:
            return None

        from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram

        program = QOpenGLShaderProgram()
        shader = QOpenGLShader(QOpenGLShader.ShaderTypeBit.Compute)
        if not shader.compileSourceCode(self._compute_shader_source()):
            message = shader.log() or "unknown compute shader error"
            raise RuntimeError(f"Failed to compile OpenGL compute shader: {message}")
        program.addShader(shader)
        if not program.link():
            message = program.log() or "unknown compute shader link error"
            raise RuntimeError(f"Failed to link OpenGL compute shader program: {message}")
        return program

    @staticmethod
    def _compute_shader_source() -> str:
        """Return the GLSL source mirroring the GPU statistics helper."""

        return (
            "#version 430\n"
            "layout(local_size_x=16, local_size_y=16, local_size_z=1) in;\n"
            "layout(binding=0) uniform sampler2D uTex;\n"
            "struct GroupStats {\n"
            "  float sumS;\n"
            "  float sumLinR;\n"
            "  float sumLinG;\n"
            "  float sumLinB;\n"
            "  uint countN;\n"
            "  uint countVHi;\n"
            "  uint countVLo;\n"
            "  uint countSkin;\n"
            "  uint hist[64];\n"
            "};\n"
            "layout(std430, binding=1) buffer StatsBuf { GroupStats gs[]; };\n"
            "shared float s_sumS;\n"
            "shared float s_sumLinR;\n"
            "shared float s_sumLinG;\n"
            "shared float s_sumLinB;\n"
            "shared uint s_countN;\n"
            "shared uint s_countVHi;\n"
            "shared uint s_countVLo;\n"
            "shared uint s_countSkin;\n"
            "shared uint s_hist[64];\n"
            "vec3 to_linear(vec3 x){\n"
            "  const float a = 0.055;\n"
            "  vec3 y;\n"
            "  for(int i=0;i<3;i++){\n"
            "    if(x[i] <= 0.04045){\n"
            "      y[i] = x[i] / 12.92;\n"
            "    } else {\n"
            "      y[i] = pow((x[i] + a) / (1.0 + a), 2.4);\n"
            "    }\n"
            "  }\n"
            "  return y;\n"
            "}\n"
            "vec3 rgb2hsv(vec3 c){\n"
            "  float r=c.r,g=c.g,b=c.b;\n"
            "  float mx = max(r, max(g,b));\n"
            "  float mn = min(r, min(g,b));\n"
            "  float d  = mx - mn + 1e-8;\n"
            "  float h = 0.0;\n"
            "  if(mx==r){ h = mod((g-b)/d, 6.0); }\n"
            "  else if(mx==g){ h = ((b-r)/d) + 2.0; }\n"
            "  else{ h = ((r-g)/d) + 4.0; }\n"
            "  h /= 6.0;\n"
            "  float s = d/(mx+1e-8);\n"
            "  float v = mx;\n"
            "  return vec3(h,s,v);\n"
            "}\n"
            "void main(){\n"
            "  uvec2 gid = gl_WorkGroupID.xy;\n"
            "  uvec2 lid = gl_LocalInvocationID.xy;\n"
            "  uvec2 gsz = gl_NumWorkGroups.xy;\n"
            "  uint groupIndex = gid.y*gsz.x + gid.x;\n"
            "  if(lid.x==0 && lid.y==0){\n"
            "    s_sumS = 0.0;\n"
            "    s_sumLinR = 0.0;\n"
            "    s_sumLinG = 0.0;\n"
            "    s_sumLinB = 0.0;\n"
            "    s_countN = 0u;\n"
            "    s_countVHi = 0u;\n"
            "    s_countVLo = 0u;\n"
            "    s_countSkin = 0u;\n"
            "    for(int i=0;i<64;i++){ s_hist[i] = 0u; }\n"
            "  }\n"
            "  barrier();\n"
            "  ivec2 size = textureSize(uTex, 0);\n"
            "  ivec2 base = ivec2(gid * uvec2(16,16));\n"
            "  ivec2 p = base + ivec2(lid);\n"
            "  if(p.x < size.x && p.y < size.y){\n"
            "    vec3 srgb = texelFetch(uTex, p, 0).rgb;\n"
            "    vec3 hsv = rgb2hsv(srgb);\n"
            "    float S = hsv.g;\n"
            "    float V = hsv.b;\n"
            "    vec3 lin = to_linear(srgb);\n"
            "    atomicAdd(s_countN, 1u);\n"
            "    if(V > 0.90){ atomicAdd(s_countVHi, 1u); }\n"
            "    if(V < 0.05){ atomicAdd(s_countVLo, 1u); }\n"
            "    float Hdeg = hsv.r * 360.0;\n"
            "    if(Hdeg>10.0 && Hdeg<50.0 && S>0.1 && S<0.6){ atomicAdd(s_countSkin, 1u); }\n"
            "    int bin = int(clamp(floor(S*64.0), 0.0, 63.0));\n"
            "    atomicAdd(s_hist[bin], 1u);\n"
            "    s_sumS += S;\n"
            "    s_sumLinR += lin.r;\n"
            "    s_sumLinG += lin.g;\n"
            "    s_sumLinB += lin.b;\n"
            "  }\n"
            "  barrier();\n"
            "  if(lid.x==0 && lid.y==0){\n"
            "    gs[groupIndex].sumS = s_sumS;\n"
            "    gs[groupIndex].sumLinR = s_sumLinR;\n"
            "    gs[groupIndex].sumLinG = s_sumLinG;\n"
            "    gs[groupIndex].sumLinB = s_sumLinB;\n"
            "    gs[groupIndex].countN = s_countN;\n"
            "    gs[groupIndex].countVHi = s_countVHi;\n"
            "    gs[groupIndex].countVLo = s_countVLo;\n"
            "    gs[groupIndex].countSkin = s_countSkin;\n"
            "    for(int i=0;i<64;i++){ gs[groupIndex].hist[i] = s_hist[i]; }\n"
            "  }\n"
            "}\n"
        )

    @classmethod
    def is_available(cls) -> bool:
        """Return ``True`` if an OpenGL rendering context is available."""

        try:
            from PySide6.QtGui import QOffscreenSurface
            from PySide6.QtGui import QOpenGLContext, QSurfaceFormat
        except Exception:
            return False

        try:
            context: QOpenGLContext | None = None
            format_hint: QSurfaceFormat | None = None
            for major, minor in ((4, 3), (3, 3)):
                candidate = QOpenGLContext()
                hint = QSurfaceFormat()
                hint.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
                hint.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
                hint.setVersion(major, minor)
                candidate.setFormat(hint)
                if candidate.create():
                    context = candidate
                    format_hint = hint
                    break
            if context is None or format_hint is None:
                return False

            surface = QOffscreenSurface()
            surface.setFormat(format_hint)
            surface.create()
            if not surface.isValid():
                return False

            if not context.makeCurrent(surface):
                return False

            # Allocate a trivial texture to confirm that the driver issues a
            # usable identifier.  Some Windows configurations report success
            # when creating the context yet fail during the first real texture
            # upload, so probing here avoids triggering runtime errors inside
            # the edit preview pipeline.
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
            return _OpenGlPreviewSession(0, 0, 0, None, ColorStats())

        if not self._make_current():
            raise RuntimeError("Failed to activate OpenGL context for session creation")

        converted = image.convertToFormat(QtImage.Format.Format_RGBA8888)
        width = converted.width()
        height = converted.height()

        texture_id = self._generate_texture()
        self._upload_texture(texture_id, converted)

        framebuffer = QOpenGLFramebufferObject(width, height)

        stats = self._compute_session_stats(texture_id, width, height, converted)

        self._context.doneCurrent()

        return _OpenGlPreviewSession(width, height, texture_id, framebuffer, stats)

    def _generate_texture(self) -> int:
        """Create and return a new OpenGL texture identifier."""

        texture_ids = (ctypes.c_uint * 1)()
        self._gl.glGenTextures(1, texture_ids)
        return int(texture_ids[0])

    def _upload_texture(self, texture_id: int, image: QImage) -> None:
        """Upload *image* data to the GPU texture identified by *texture_id*."""

        if texture_id == 0:
            raise RuntimeError("Invalid OpenGL texture identifier")

        # ``bits()`` returns a sip.voidptr.  Requesting the full buffer size
        # ensures Qt detaches the underlying storage so the upload observes a
        # stable snapshot of the pixels even if the caller modifies the source
        # image later on.
        buffer = image.bits()
        buffer.setsize(image.sizeInBytes())

        gl = self._gl
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexImage2D(
            gl.GL_TEXTURE_2D,
            0,
            gl.GL_RGBA,
            image.width(),
            image.height(),
            0,
            gl.GL_RGBA,
            gl.GL_UNSIGNED_BYTE,
            buffer,
        )
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    def _compute_session_stats(
        self,
        texture_id: int,
        width: int,
        height: int,
        fallback_image: QImage,
    ) -> ColorStats:
        """Return :class:`ColorStats` using the compute shader when available."""

        fallback_stats: ColorStats | None = None

        def _fallback() -> ColorStats:
            nonlocal fallback_stats
            if fallback_stats is None:
                fallback_stats = compute_color_statistics(fallback_image)
            return fallback_stats

        if (
            texture_id == 0
            or width == 0
            or height == 0
            or self._compute_program is None
            or self._stats_buffer is None
            or self._gl43 is None
        ):
            return _fallback()

        groups_x = (width + 15) // 16
        groups_y = (height + 15) // 16
        group_count = max(groups_x * groups_y, 1)
        stride = 320
        total_size = stride * group_count

        if not self._stats_buffer.bind():
            return _fallback()
        # Allocate or resize the buffer to hold one record per work group.
        self._stats_buffer.allocate(total_size)
        self._stats_buffer.release()

        gl = self._gl
        gl43 = self._gl43
        program = self._compute_program
        assert program is not None  # guarded above

        if not program.bind():
            return _fallback()

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)
        program.setUniformValue("uTex", 0)

        buffer_id = self._stats_buffer.bufferId()
        gl43.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 1, buffer_id)
        gl43.glDispatchCompute(groups_x, groups_y, 1)
        gl43.glMemoryBarrier(gl.GL_SHADER_STORAGE_BARRIER_BIT | gl.GL_BUFFER_UPDATE_BARRIER_BIT)

        program.release()
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

        if not self._stats_buffer.bind():
            return _fallback()

        mapped = self._stats_buffer.mapRange(
            0,
            total_size,
            QOpenGLBuffer.RangeAccessFlag.ReadAccess,
        )
        if mapped is None:
            self._stats_buffer.release()
            return _fallback()

        raw = bytes(mapped)
        self._stats_buffer.unmap()
        self._stats_buffer.release()

        # Some drivers align SSBO records beyond the declared payload.  Derive the
        # actual stride from the returned data to ensure we walk the buffer
        # correctly on all platforms.
        if len(raw) // group_count > stride:
            stride = len(raw) // group_count

        sum_saturation = 0.0
        sum_lin_r = 0.0
        sum_lin_g = 0.0
        sum_lin_b = 0.0
        count = 0
        highlight_count = 0
        dark_count = 0
        skin_count = 0
        histogram = [0] * 64

        for index in range(group_count):
            base = index * stride
            if base + 288 > len(raw):
                break
            sum_saturation += struct.unpack_from("<f", raw, base + 0x00)[0]
            sum_lin_r += struct.unpack_from("<f", raw, base + 0x04)[0]
            sum_lin_g += struct.unpack_from("<f", raw, base + 0x08)[0]
            sum_lin_b += struct.unpack_from("<f", raw, base + 0x0C)[0]
            count += struct.unpack_from("<I", raw, base + 0x10)[0]
            highlight_count += struct.unpack_from("<I", raw, base + 0x14)[0]
            dark_count += struct.unpack_from("<I", raw, base + 0x18)[0]
            skin_count += struct.unpack_from("<I", raw, base + 0x1C)[0]
            hist_slice = struct.unpack_from("<64I", raw, base + 0x20)
            for bin_index, bin_value in enumerate(hist_slice):
                histogram[bin_index] += bin_value

        if count == 0:
            return ColorStats()

        mean_saturation = sum_saturation / count
        cumulative = 0
        median_target = count // 2
        median_saturation = 0.0
        for bin_index, bin_value in enumerate(histogram):
            cumulative += bin_value
            if cumulative >= median_target:
                median_saturation = (bin_index + 0.5) / 64.0
                break

        highlight_ratio = highlight_count / count
        dark_ratio = dark_count / count
        skin_ratio = skin_count / count

        avg_lin_r = sum_lin_r / count
        avg_lin_g = sum_lin_g / count
        avg_lin_b = sum_lin_b / count
        avg_lin = (avg_lin_r + avg_lin_g + avg_lin_b) / 3.0

        def _safe_gain(value: float) -> float:
            if value <= 1e-6:
                return 1.0
            return avg_lin / value

        gain_r = max(0.5, min(2.5, _safe_gain(avg_lin_r)))
        gain_g = max(0.5, min(2.5, _safe_gain(avg_lin_g)))
        gain_b = max(0.5, min(2.5, _safe_gain(avg_lin_b)))

        cast_magnitude = max(
            abs(avg_lin_r - avg_lin),
            abs(avg_lin_g - avg_lin),
            abs(avg_lin_b - avg_lin),
        )

        return ColorStats(
            saturation_mean=min(max(mean_saturation, 0.0), 1.0),
            saturation_median=min(max(median_saturation, 0.0), 1.0),
            highlight_ratio=min(max(highlight_ratio, 0.0), 1.0),
            dark_ratio=min(max(dark_ratio, 0.0), 1.0),
            skin_ratio=min(max(skin_ratio, 0.0), 1.0),
            cast_magnitude=min(max(cast_magnitude, 0.0), 1.0),
            white_balance_gain=(gain_r, gain_g, gain_b),
        )

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
        saturation = float(adjustments.get("Saturation", 0.0))
        vibrance = float(adjustments.get("Vibrance", 0.0))
        cast = float(adjustments.get("Cast", 0.0))
        if (
            "Color_Gain_R" in adjustments
            and "Color_Gain_G" in adjustments
            and "Color_Gain_B" in adjustments
        ):
            gain_r = float(adjustments.get("Color_Gain_R", 1.0))
            gain_g = float(adjustments.get("Color_Gain_G", 1.0))
            gain_b = float(adjustments.get("Color_Gain_B", 1.0))
        else:
            gain_r, gain_g, gain_b = gl_session.color_stats.white_balance_gain

        program.setUniformValue(self._uniform_exposure, exposure_term)
        program.setUniformValue(self._uniform_brightness, brightness_term)
        program.setUniformValue(self._uniform_brilliance, brilliance_strength)
        program.setUniformValue(self._uniform_highlights, highlights)
        program.setUniformValue(self._uniform_shadows, shadows)
        program.setUniformValue(self._uniform_contrast, contrast_factor)
        program.setUniformValue(self._uniform_black_point, black_point)
        program.setUniformValue(self._uniform_saturation, saturation)
        program.setUniformValue(self._uniform_vibrance, vibrance)
        program.setUniformValue(self._uniform_cast, cast)
        program.setUniformValue(self._uniform_gain, gain_r, gain_g, gain_b)

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
            if gl_session.texture_id != 0:
                texture_ids = (ctypes.c_uint * 1)(gl_session.texture_id)
                self._gl.glDeleteTextures(1, texture_ids)
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
    color_stats: ColorStats

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
