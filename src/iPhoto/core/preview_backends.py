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

logging.basicConfig(level=logging.DEBUG)
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
        from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram

        super().__init__()

        self._context: QOpenGLContext = QOpenGLContext()
        # If another GL context is already current (for example the map
        # renderer), request that our context shares resources with it so
        # textures and other GL objects can be shared safely and avoid
        # driver-level conflicts. setShareContext must be called before
        # create(). If no context exists this is a no-op.
        try:
            current = QOpenGLContext.currentContext()
            if current is not None:
                # setShareContext is a soft hint; some platforms may ignore it
                # but most Qt drivers will enable resource sharing between
                # compatible contexts.
                self._context.setShareContext(current)
        except Exception:
            # Defensive: if sharing fails, continue with an isolated context.
            pass
        format_hint = QSurfaceFormat()
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

        functions = self._context.functions()
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
        self._uniform_saturation = self._program.uniformLocation("uSaturation")
        self._uniform_vibrance = self._program.uniformLocation("uVibrance")
        self._uniform_cast = self._program.uniformLocation("uCast")
        self._uniform_color_gain = self._program.uniformLocation("uColorGain")

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

        # Try to compile a compute shader for fast GPU statistics. This is
        # optional: if the driver or Qt binding does not support compute
        # shaders we silently fall back to CPU defaults.  The compilation and
        # buffer creation must happen while the OpenGL context is current.
        self._compute_program = None
        self._ssbo = 0
        try:
            cs_src = self._compute_shader_source()
            cs = QOpenGLShader(QOpenGLShader.ShaderTypeBit.Compute)
            if not cs.compileSourceCode(cs_src):
                raise RuntimeError(cs.log() or "compute shader compile failed")
            compute_prog = QOpenGLShaderProgram()
            compute_prog.addShader(cs)
            if not compute_prog.link():
                raise RuntimeError(compute_prog.log() or "compute shader link failed")
            self._compute_program = compute_prog
            # Create SSBO handle while context is current
            buf_ids = (ctypes.c_uint * 1)()
            self._gl.glGenBuffers(1, buf_ids)
            self._ssbo = int(buf_ids[0])
            _LOGGER.info("OpenGL compute shader initialized (GPU stats enabled)")
        except Exception as ex:
            self._compute_program = None
            self._ssbo = 0
            _LOGGER.info("OpenGL compute shader unavailable, falling back to CPU stats: %s", ex)

        self._context.doneCurrent()

    @staticmethod
    def _vertex_shader_source() -> str:
        """Return the GLSL source code for the fullscreen quad vertex shader."""

        return (
            "#version 120\n"
            "attribute vec2 a_position;\n"
            "attribute vec2 a_texcoord;\n"
            "varying vec2 v_texcoord;\n"
            "void main() {\n"
            "    gl_Position = vec4(a_position, 0.0, 1.0);\n"
            "    v_texcoord = a_texcoord;\n"
            "}\n"
        )

    @staticmethod
    def _fragment_shader_source() -> str:
        """Return the GLSL source code mirroring ``_apply_channel_adjustments``."""

        # The fragment merges the existing per-channel "apply_channel" logic
        # (exposure/brightness/brilliance/highlights/shadows/contrast/black)
        # with the color-widget's saturation/vibrance/cast/gain pipeline. It
        # uses GLSL 1.20-compatible constructs (attribute/varying/texture2D)
        # so the shader compiles on a wide range of drivers while keeping the
        # same uniform names the backend expects.
        return (
            "#version 120\n"
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
            "uniform vec3 uColorGain;\n"
            "varying vec2 v_texcoord;\n"
            "float clamp01(float value) { return clamp(value, 0.0, 1.0); }\n"
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
            "    vec4 tex_color = texture2D(uSourceTexture, v_texcoord);\n"
            "    // Per-channel tone adjustments (keeps the original pipeline)\n"
            "    tex_color.r = apply_channel(tex_color.r);\n"
            "    tex_color.g = apply_channel(tex_color.g);\n"
            "    tex_color.b = apply_channel(tex_color.b);\n"
            "    // Apply color cast / white-balance gains (mix with identity by uCast)\n"
            "    vec3 color = tex_color.rgb * mix(vec3(1.0), uColorGain, clamp(uCast, 0.0, 1.0));\n"
            "    // Saturation + Vibrance (color_widget algorithm)\n"
            "    float luma = dot(color, vec3(0.299, 0.587, 0.114));\n"
            "    vec3 chroma = color - vec3(luma);\n"
            "    float satAmt = 1.0 + uSaturation;\n"
            "    float vibAmt = 1.0 + uVibrance;\n"
            "    float w = 1.0 - clamp(abs(luma - 0.5) * 2.0, 0.0, 1.0);\n"
            "    chroma *= satAmt * mix(1.0, vibAmt, w);\n"
            "    tex_color.rgb = clamp(vec3(luma) + chroma, 0.0, 1.0);\n"
            "    gl_FragColor = tex_color;\n"
            "}\n"
        )

    @staticmethod
    def _compute_shader_source() -> str:
        """Compute shader for per-group statistics (ported from color_widget)."""
        return (
            "#version 430\n"
            "layout(local_size_x=16, local_size_y=16, local_size_z=1) in;\n"
            "layout(binding=0) uniform sampler2D uTex;\n"
            "struct GroupStats {\n"
            "  float sumS;\n"
            "  float sumLinR;\n"
            "  float sumLinG;\n"
            "  float sumLinB;\n"
            "  uint  countN;\n"
            "  uint  countVHi;\n"
            "  uint  countVLo;\n"
            "  uint  countSkin;\n"
            "  uint  hist[64];\n"
            "};\n"
            "layout(std430, binding=1) buffer StatsBuf { GroupStats gs[]; };\n"
            "shared float s_sumS;\n"
            "shared float s_sumLinR, s_sumLinG, s_sumLinB;\n"
            "shared uint  s_countN, s_countVHi, s_countVLo, s_countSkin;\n"
            "shared uint  s_hist[64];\n"
            "vec3 to_linear(vec3 x){\n"
            "  const float a = 0.055;\n"
            "  vec3 y;\n"
            "  for(int i=0;i<3;i++){\n"
            "    y[i] = (x[i] <= 0.04045) ? (x[i]/12.92) : pow((x[i]+a)/(1.0+a), 2.4);\n"
            "  }\n"
            "  return y;\n"
            "}\n"
            "vec3 rgb2hsv(vec3 c){\n"
            "  float r=c.r,g=c.g,b=c.b;\n"
            "  float mx = max(r, max(g,b));\n"
            "  float mn = min(r, min(g,b));\n"
            "  float d  = mx - mn + 1e-8;\n"
            "  float h = 0.0;\n"
            "  if(mx==r)      h = mod((g-b)/d, 6.0);\n"
            "  else if(mx==g) h = ((b-r)/d) + 2.0;\n"
            "  else           h = ((r-g)/d) + 4.0;\n"
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
            "    s_sumLinR = s_sumLinG = s_sumLinB = 0.0;\n"
            "    s_countN = s_countVHi = s_countVLo = s_countSkin = 0u;\n"
            "    for(int i=0;i<64;i++) s_hist[i] = 0u;\n"
            "  }\n"
            "  barrier();\n"
            "  ivec2 size = textureSize(uTex, 0);\n"
            "  ivec2 base = ivec2(gid * uvec2(16,16));\n"
            "  ivec2 p = base + ivec2(lid);\n"
            "  if(p.x < size.x && p.y < size.y){\n"
            "    vec3 srgb = texelFetch(uTex, p, 0).rgb;\n"
            "    vec3 hsv  = rgb2hsv(srgb);\n"
            "    float S = hsv.g, V = hsv.b;\n"
            "    vec3 lin = to_linear(srgb);\n"
            "    atomicAdd(s_countN, 1u);\n"
            "    if(V > 0.90) atomicAdd(s_countVHi, 1u);\n"
            "    if(V < 0.05) atomicAdd(s_countVLo, 1u);\n"
            "    float Hdeg = hsv.r * 360.0;\n"
            "    if(Hdeg>10.0 && Hdeg<50.0 && S>0.1 && S<0.6) atomicAdd(s_countSkin, 1u);\n"
            "    int bin = int(clamp(floor(S*64.0), 0.0, 63.0));\n"
            "    atomicAdd(s_hist[bin], 1u);\n"
            "    s_sumS    += S;\n"
            "    s_sumLinR += lin.r;\n"
            "    s_sumLinG += lin.g;\n"
            "    s_sumLinB += lin.b;\n"
            "  }\n"
            "  barrier();\n"
            "  if(lid.x==0 && lid.y==0){\n"
            "    gs[groupIndex].sumS    = s_sumS;\n"
            "    gs[groupIndex].sumLinR = s_sumLinR;\n"
            "    gs[groupIndex].sumLinG = s_sumLinG;\n"
            "    gs[groupIndex].sumLinB = s_sumLinB;\n"
            "    gs[groupIndex].countN  = s_countN;\n"
            "    gs[groupIndex].countVHi= s_countVHi;\n"
            "    gs[groupIndex].countVLo= s_countVLo;\n"
            "    gs[groupIndex].countSkin = s_countSkin;\n"
            "    for(int i=0;i<64;i++) gs[groupIndex].hist[i] = s_hist[i];\n"
            "  }\n"
            "}\n"
        )

    def compute_stats(self, texture_id: int, W: int, H: int):
        """Run the GPU compute shader to gather image statistics.

        Returns a tuple equivalent to color_widget: (Sbar, S50, Hpct, Dpct,
        SkinPct, CastMag, gains(np.array[3], dtype=float32)).  Falls back to
        simple defaults when compute is unavailable or an error occurs.
        """
        if self._compute_program is None or self._ssbo == 0:
            # Fallback defaults mirroring color_widget's CPU fallback
            return (0.5, 0.5, 0.0, 0.0, 0.0, 0.0, (1.0, 1.0, 1.0))

        gl = self._gl
        gx, gy = (W + 15) // 16, (H + 15) // 16
        group_count = gx * gy

        # Allocate SSBO storage (320 bytes per group as a safe over-allocation)
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, self._ssbo)
        total_alloc = max(320 * group_count, 320)
        gl.glBufferData(gl.GL_SHADER_STORAGE_BUFFER, total_alloc, None, gl.GL_DYNAMIC_READ)
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, 0)

        # Bind program and dispatch
        prog = self._compute_program
        if not prog.bind():
            return (0.5, 0.5, 0.0, 0.0, 0.0, 0.0, (1.0, 1.0, 1.0))

        # set sampler uniform to texture unit 0
        try:
            prog.setUniformValue("uTex", 0)
        except Exception:
            # Some Qt versions require different uniform setting; ignore if it fails
            pass

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)
        gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 1, self._ssbo)

        # Dispatch and synchronize
        gl.glDispatchCompute(gx, gy, 1)
        gl.glMemoryBarrier(gl.GL_SHADER_STORAGE_BARRIER_BIT | gl.GL_BUFFER_UPDATE_BARRIER_BIT)

        # Read back SSBO
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, self._ssbo)
        total_size = gl.glGetBufferParameteriv(gl.GL_SHADER_STORAGE_BUFFER, gl.GL_BUFFER_SIZE)
        if not isinstance(total_size, int):
            total_size = int(total_size)
        ptr = gl.glMapBuffer(gl.GL_SHADER_STORAGE_BUFFER, gl.GL_READ_ONLY)
        if not ptr:
            gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, 0)
            return (0.5, 0.5, 0.0, 0.0, 0.0, 0.0, (1.0, 1.0, 1.0))

        raw = ctypes.string_at(ptr, total_size)
        gl.glUnmapBuffer(gl.GL_SHADER_STORAGE_BUFFER)
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, 0)

        # Aggregate groups (port of color_widget python aggregation)
        stride = total_size // max(group_count, 1)
        if stride < 288:
            stride = 288

        import numpy as _np

        sumS = 0.0
        sumLinR = sumLinG = sumLinB = 0.0
        countN = countVHi = countVLo = countSkin = 0
        hist = _np.zeros(64, dtype=_np.uint64)

        for i in range(group_count):
            base = i * stride
            if base + 288 > len(raw):
                break
            s_sumS = _np.frombuffer(raw, dtype='<f4', count=1, offset=base + 0x00)[0]
            s_sumLinR = _np.frombuffer(raw, dtype='<f4', count=1, offset=base + 0x04)[0]
            s_sumLinG = _np.frombuffer(raw, dtype='<f4', count=1, offset=base + 0x08)[0]
            s_sumLinB = _np.frombuffer(raw, dtype='<f4', count=1, offset=base + 0x0c)[0]
            s_countN = _np.frombuffer(raw, dtype='<u4', count=1, offset=base + 0x10)[0]
            s_countVHi = _np.frombuffer(raw, dtype='<u4', count=1, offset=base + 0x14)[0]
            s_countVLo = _np.frombuffer(raw, dtype='<u4', count=1, offset=base + 0x18)[0]
            s_countSkin = _np.frombuffer(raw, dtype='<u4', count=1, offset=base + 0x1c)[0]
            s_hist = _np.frombuffer(raw, dtype='<u4', count=64, offset=base + 0x20)

            sumS += float(s_sumS)
            sumLinR += float(s_sumLinR)
            sumLinG += float(s_sumLinG)
            sumLinB += float(s_sumLinB)
            countN += int(s_countN)
            countVHi += int(s_countVHi)
            countVLo += int(s_countVLo)
            countSkin += int(s_countSkin)
            hist += s_hist.astype(_np.uint64)

        if countN == 0:
            return (0.5, 0.5, 0.0, 0.0, 0.0, 0.0, (1.0, 1.0, 1.0))

        Sbar = sumS / countN
        cumsum = _np.cumsum(hist)
        half = countN // 2
        bin_idx = int(_np.searchsorted(cumsum, half))
        bin_idx = min(max(bin_idx, 0), 63)
        S50 = (bin_idx + 0.5) / 64.0

        Hpct = countVHi / countN
        Dpct = countVLo / countN
        SkinPct = countSkin / countN

        means = _np.array([sumLinR, sumLinG, sumLinB], dtype=_np.float64) / max(countN, 1)
        means += 1e-8
        m = float(_np.mean(means))
        gains = (m / means).astype(_np.float32)
        CastMag = float(_np.linalg.norm(_np.log(gains)))

        return (float(Sbar), float(S50), float(Hpct), float(Dpct), float(SkinPct), float(CastMag), gains)

    def aggregate_params_from_stats(self, stats, t: float):
        """Map GPU stats to automatic sat/vib/cast/gain parameters.

        Ported logic from color_widget.aggregate_params_from_stats.
        """
        import numpy as _np

        Sbar, S50, Hpct, Dpct, SkinPct, CastMag, gains = stats

        def smoothstep(e0, e1, x):
            t0 = _np.clip((x - e0) / (e1 - e0 + 1e-8), 0, 1)
            return t0 * t0 * (3 - 2 * t0)

        k_hi = max(0.35, 1 - smoothstep(0.02, 0.15, Hpct))
        k_skin = 0.6 + 0.4 * (1 - _np.clip(SkinPct, 0, 1))
        k_sat0 = (1 - S50) ** 0.6
        k_vib0 = (1 - Sbar)

        base_sat = 0.25 + 0.75 * k_sat0
        base_vib = 0.25 + 0.75 * k_vib0
        AMP_SAT, AMP_VIB = 1.6, 1.4

        sat = AMP_SAT * 0.9 * t * base_sat * k_hi * k_skin
        vib = AMP_VIB * 0.7 * t * base_vib * k_hi * k_skin
        cast = 0.8 * abs(t) * _np.clip(CastMag / 0.4, 0, 1)

        eps = 0.01 * max(0.0, abs(t))
        if abs(sat) < eps:
            sat = (t and (sat / abs(sat))) or eps
        if abs(vib) < eps:
            vib = (t and (vib / abs(vib))) or eps

        sat = float(_np.clip(sat, -1, 1))
        vib = float(_np.clip(vib, -1, 1))
        cast = float(_np.clip(cast, 0.0, 1.0))

        # sanitize gains
        try:
            gains_arr = _np.asarray(gains, dtype=_np.float32).copy()
            if gains_arr.size < 3:
                gains_arr = _np.ones(3, dtype=_np.float32)
            gains_arr[~_np.isfinite(gains_arr)] = 1.0
        except Exception:
            gains_arr = _np.ones(3, dtype=_np.float32)

        return sat, vib, cast, gains_arr

    @classmethod
    def is_available(cls) -> bool:
        """Return ``True`` if an OpenGL rendering context is available."""

        try:
            from PySide6.QtGui import QOffscreenSurface
            from PySide6.QtGui import QOpenGLContext, QSurfaceFormat
        except Exception:
            return False

        # If another module (for example the map renderer) already created a
        # current context, prefer using it for availability probing. This
        # avoids situations where creating a fresh offscreen context fails on
        # platforms with a single-context driver or when contexts must be
        # created with specific profiles.
        try:
            current = QOpenGLContext.currentContext()
            if current is not None and current.isValid():
                try:
                    funcs = current.functions()
                    tex = (ctypes.c_uint * 1)()
                    funcs.glGenTextures(1, tex)
                    texid = int(tex[0])
                    if texid == 0:
                        return False
                    funcs.glDeleteTextures(1, tex)
                    return True
                except Exception:
                    # Fall through to creating an offscreen context
                    pass
        except Exception:
            # If querying the current context fails, fall back to the usual
            # probe which creates an offscreen context.
            pass

        def try_format(major: int, minor: int, core_profile: bool) -> bool:
            ctx = None
            try:
                ctx = QOpenGLContext()
                fmt = QSurfaceFormat()
                fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
                fmt.setVersion(major, minor)
                if core_profile:
                    fmt.setProfile(QSurfaceFormat.CoreProfile)
                ctx.setFormat(fmt)
                if not ctx.create():
                    return False
                surf = QOffscreenSurface()
                surf.setFormat(ctx.format())
                surf.create()
                if not surf.isValid():
                    return False
                if not ctx.makeCurrent(surf):
                    return False
                funcs = ctx.functions()
                tex = (ctypes.c_uint * 1)()
                funcs.glGenTextures(1, tex)
                tid = int(tex[0])
                if tid == 0:
                    return False
                funcs.glDeleteTextures(1, tex)
                return True
            except Exception:
                return False
            finally:
                try:
                    if ctx is not None:
                        ctx.doneCurrent()
                except Exception:
                    pass

        # Try modern core profiles first (map renderer may require these)
        candidates = [
            (4, 6, True),
            (4, 3, True),
            (3, 3, True),
            (2, 0, False),  # compatibility fallback
        ]
        for major, minor, core in candidates:
            try:
                if try_format(major, minor, core):
                    return True
            except Exception:
                continue

        return False

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

        texture_id = self._generate_texture()
        self._upload_texture(texture_id, converted)

        framebuffer = QOpenGLFramebufferObject(width, height)

        # If compute is available, produce GPU statistics and default auto
        # adjustments.  Store them on the session for controllers to consume.
        stats = None
        auto = None
        try:
            stats = self.compute_stats(texture_id, width, height)
            sat, vib, cast, gains_arr = self.aggregate_params_from_stats(stats, 0.0)
            auto = {
                "Saturation": sat,
                "Vibrance": vib,
                "Cast": cast,
                "Color_Gain_R": float(gains_arr[0]),
                "Color_Gain_G": float(gains_arr[1]),
                "Color_Gain_B": float(gains_arr[2]),
            }
        except Exception:
            stats = None
            auto = None

        self._context.doneCurrent()
        return _OpenGlPreviewSession(width, height, texture_id, framebuffer, stats, auto)

    def _generate_texture(self) -> int:
        """Create and return a new OpenGL texture identifier."""

        texture_ids = (ctypes.c_uint * 1)()
        self._gl.glGenTextures(1, texture_ids)
        return int(texture_ids[0])

    def _cleanup_gl_resources(self) -> None:
        """Free GL resources owned by the backend (SSBO, VBO)."""
        try:
            if not self._make_current():
                return
            gl = self._gl
            # Delete SSBO
            try:
                if getattr(self, "_ssbo", 0):
                    buf = (ctypes.c_uint * 1)(self._ssbo)
                    gl.glDeleteBuffers(1, buf)
                    self._ssbo = 0
            except Exception:
                pass
            # Destroy vertex buffer
            try:
                if getattr(self, "_vertex_buffer", None) is not None:
                    try:
                        self._vertex_buffer.destroy()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self._context.doneCurrent()
            except Exception:
                pass
        except Exception:
            pass

    def __del__(self):
        # Attempt best-effort cleanup of GL resources.
        try:
            self._cleanup_gl_resources()
        except Exception:
            pass

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
        gain_r = float(adjustments.get("Color_Gain_R", 1.0))
        gain_g = float(adjustments.get("Color_Gain_G", 1.0))
        gain_b = float(adjustments.get("Color_Gain_B", 1.0))

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
        program.setUniformValue(self._uniform_color_gain, gain_r, gain_g, gain_b)

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
    # GPU-derived statistics (Sbar, S50, Hpct, Dpct, SkinPct, CastMag, gains)
    stats: "tuple | None" = None
    # Cached automatic adjustments derived from stats (dict with keys used
    # by controller: Saturation, Vibrance, Cast, Color_Gain_R/G/B)
    auto_adjustments: "dict | None" = None

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

    # OpenGL is the next best choice when CUDA is not available.  Try to
    # construct the backend directly; some platforms create the useful GL
    # context later in the application lifecycle (for example the map widget)
    # so probing earlier may return a false negative. If instantiation fails
    # we fall back to the CPU backend.
    try:
        backend = _OpenGlPreviewBackend()
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.info("OpenGL preview backend unavailable, falling back: %s", exc)
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
