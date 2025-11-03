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
    from PySide6.QtGui import QSurfaceFormat
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

        _LOGGER.debug("Disposing session: %s", session)
        session.dispose()


@dataclass
class _CpuPreviewSession(PreviewSession):
    """Store the original image and precomputed statistics for the CPU backend."""

    image: QImage
    color_stats: ColorStats

    def dispose(self) -> None:  # pragma: no cover - nothing to free
        """Release held resources (no-op for pure CPU sessions)."""

        _LOGGER.debug("Disposing _CpuPreviewSession. No-op.")
        # No explicit resource management is required for the CPU fallback.  The
        # controller simply drops the reference to the session, allowing Python's
        # garbage collector to reclaim the implicit ``QImage`` copy naturally.
        return


class _CpuPreviewBackend(PreviewBackend):
    """CPU implementation using the existing tone-mapping helpers."""

    tier_name = "CPU"
    supports_realtime = False

    def create_session(self, image: QImage) -> PreviewSession:
        _LOGGER.debug("CPUBackend: Creating session.")
        stats = compute_color_statistics(image) if not image.isNull() else ColorStats()
        return _CpuPreviewSession(image, stats)

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:
        _LOGGER.debug("CPUBackend: Rendering.")
        assert isinstance(session, _CpuPreviewSession)
        return apply_adjustments(session.image, adjustments, color_stats=session.color_stats)


class _CudaPreviewBackend(PreviewBackend):
    """Placeholder for a future CUDA accelerated implementation."""

    tier_name = "CUDA"
    supports_realtime = True

    def __init__(self) -> None:
        _LOGGER.error("CUDA backend __init__ called but it is not implemented.")
        raise RuntimeError("CUDA backend is not implemented in this build")

    @classmethod
    def is_available(cls) -> bool:
        """Return ``True`` when the runtime provides the required CUDA stack."""

        _LOGGER.debug("Checking for CUDA availability.")
        try:
            import cupy  # type: ignore  # noqa: F401
        except ImportError:
            _LOGGER.debug("CUDA check: cupy not found.")
            return False
        _LOGGER.info("CUDA runtime detected but backend is not yet implemented; skipping")
        return False

    def create_session(self, image: QImage) -> PreviewSession:  # pragma: no cover - not reachable
        _LOGGER.error("CUDA backend create_session called but it is not implemented.")
        raise NotImplementedError

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:  # pragma: no cover - not reachable
        _LOGGER.error("CUDA backend render called but it is not implemented.")
        raise NotImplementedError


class _OpenGlPreviewBackend(PreviewBackend):
    """OpenGL based preview backend using GLSL shaders for tone mapping."""

    tier_name = "OpenGL"
    supports_realtime = True

    @staticmethod
    def _candidate_formats(
        surface_format_cls: type["QSurfaceFormat"],
    ) -> list["QSurfaceFormat"]:
        """Return potential context formats ordered by desirability.

        The helper favours the process-wide default format first so that the
        preview backend mirrors whichever OpenGL version the rest of the
        application already requested.  Falling back to explicit versions keeps
        legacy drivers in play when no default is configured.
        """
        _LOGGER.debug("OpenGL: Determining candidate formats.")
        candidates: list["QSurfaceFormat"] = []
        default_format = surface_format_cls.defaultFormat()
        if (
            default_format.renderableType()
            == surface_format_cls.RenderableType.OpenGL
            and default_format.majorVersion() > 0
        ):
            _LOGGER.debug(
                "OpenGL: Found default format: Version %d.%d, Profile: %s",
                default_format.majorVersion(),
                default_format.minorVersion(),
                default_format.profile(),
            )
            # Copy the default format so adjustments performed later on do not
            # mutate the process-wide configuration.
            try:
                candidates.append(surface_format_cls(default_format))
            except TypeError:
                _LOGGER.warning(
                    "OpenGL: QSurfaceFormat copy constructor failed. Manual copy."
                )
                # Some bindings lack the convenience copy constructor.  In that
                # case manually mirror the relevant properties to preserve the
                # process-wide defaults.
                format_copy = surface_format_cls()
                format_copy.setRenderableType(default_format.renderableType())
                format_copy.setProfile(default_format.profile())
                format_copy.setVersion(
                    default_format.majorVersion(),
                    default_format.minorVersion(),
                )
                format_copy.setSwapBehavior(default_format.swapBehavior())
                format_copy.setSwapInterval(default_format.swapInterval())
                format_copy.setDepthBufferSize(default_format.depthBufferSize())
                format_copy.setStencilBufferSize(default_format.stencilBufferSize())
                format_copy.setSamples(default_format.samples())
                format_copy.setRedBufferSize(default_format.redBufferSize())
                format_copy.setGreenBufferSize(default_format.greenBufferSize())
                format_copy.setBlueBufferSize(default_format.blueBufferSize())
                format_copy.setAlphaBufferSize(default_format.alphaBufferSize())
                format_copy.setOption(
                    surface_format_cls.FormatOption.DebugContext,
                    default_format.testOption(
                        surface_format_cls.FormatOption.DebugContext
                    ),
                )
                candidates.append(format_copy)
        else:
            _LOGGER.debug("OpenGL: No valid default format found.")

        for major, minor in ((4, 3), (3, 3)):
            _LOGGER.debug("OpenGL: Adding candidate format: %d.%d Core Profile", major, minor)
            format_hint = surface_format_cls()
            format_hint.setRenderableType(surface_format_cls.RenderableType.OpenGL)
            format_hint.setProfile(surface_format_cls.OpenGLContextProfile.CoreProfile)
            format_hint.setVersion(major, minor)
            candidates.append(format_hint)

        return candidates

    @classmethod
    def _initialise_context(
        cls,
        context_cls: type["QOpenGLContext"],
        surface_format_cls: type["QSurfaceFormat"],
    ) -> tuple["QOpenGLContext", "QSurfaceFormat"]:
        """Create an OpenGL context matching the map view configuration.

        Passing the Qt classes explicitly keeps the helper import-agnostic and
        ensures that :meth:`is_available` can reuse the logic without eagerly
        importing OpenGL modules at the top level.
        """
        _LOGGER.debug("OpenGL: Initialising context...")
        share_context = context_cls.globalShareContext()
        last_error: Exception | None = None
        candidate_formats: list["QSurfaceFormat"] = []
        if share_context is not None:
            share_format = share_context.format()
            if (
                share_format.renderableType()
                == surface_format_cls.RenderableType.OpenGL
            ):
                _LOGGER.debug(
                    "OpenGL: Global share context found. Version %d.%d, Profile: %s",
                    share_format.majorVersion(),
                    share_format.minorVersion(),
                    share_format.profile(),
                )
                try:
                    candidate_formats.append(surface_format_cls(share_format))
                except TypeError:
                    _LOGGER.warning(
                        "OpenGL: QSurfaceFormat copy constructor failed for share_format. Manual copy."
                    )
                    cloned = surface_format_cls()
                    cloned.setRenderableType(share_format.renderableType())
                    cloned.setProfile(share_format.profile())
                    cloned.setVersion(
                        share_format.majorVersion(),
                        share_format.minorVersion(),
                    )
                    cloned.setSwapBehavior(share_format.swapBehavior())
                    cloned.setSwapInterval(share_format.swapInterval())
                    cloned.setDepthBufferSize(share_format.depthBufferSize())
                    cloned.setStencilBufferSize(share_format.stencilBufferSize())
                    cloned.setSamples(share_format.samples())
                    cloned.setRedBufferSize(share_format.redBufferSize())
                    cloned.setGreenBufferSize(share_format.greenBufferSize())
                    cloned.setBlueBufferSize(share_format.blueBufferSize())
                    cloned.setAlphaBufferSize(share_format.alphaBufferSize())
                    cloned.setOption(
                        surface_format_cls.FormatOption.DebugContext,
                        share_format.testOption(
                            surface_format_cls.FormatOption.DebugContext
                        ),
                    )
                    candidate_formats.append(cloned)
            else:
                _LOGGER.debug("OpenGL: Global share context is not OpenGL, skipping.")
        else:
            _LOGGER.debug("OpenGL: No global share context found.")

        candidate_formats.extend(cls._candidate_formats(surface_format_cls))
        if not candidate_formats:
            _LOGGER.error("OpenGL: No candidate formats found for context creation.")
            raise RuntimeError("Failed to create OpenGL context: No candidate formats.")

        _LOGGER.debug("OpenGL: Trying %d candidate formats.", len(candidate_formats))
        for i, format_hint in enumerate(candidate_formats):
            _LOGGER.debug(
                "OpenGL: Trying candidate %d: Version %d.%d, Profile: %s, Renderable: %s",
                i,
                format_hint.majorVersion(),
                format_hint.minorVersion(),
                format_hint.profile(),
                format_hint.renderableType(),
            )
            context = context_cls()
            if share_context is not None:
                context.setShareContext(share_context)
            context.setFormat(format_hint)
            try:
                if not context.create():
                    _LOGGER.warning("OpenGL: context.create() failed for candidate %d.", i)
                    continue
            except Exception as exc:  # pragma: no cover - platform specific
                _LOGGER.warning(
                    "OpenGL: context.create() raised exception for candidate %d: %s",
                    i,
                    exc,
                    exc_info=True,
                )
                last_error = exc
                continue

            actual_format = context.format()
            if (
                actual_format.renderableType()
                != surface_format_cls.RenderableType.OpenGL
            ):
                _LOGGER.warning(
                    "OpenGL: Candidate %d created context, but renderable type is not OpenGL (%s).",
                    i,
                    actual_format.renderableType(),
                )
                continue

            _LOGGER.info(
                "OpenGL: Successfully created context. Actual Version: %d.%d, Profile: %s",
                actual_format.majorVersion(),
                actual_format.minorVersion(),
                actual_format.profile(),
            )
            return context, actual_format

        message = "Failed to create OpenGL context after trying all candidates."
        _LOGGER.error(message)
        if last_error is not None:
            raise RuntimeError(message) from last_error
        raise RuntimeError(message)

    def __init__(self) -> None:
        _LOGGER.debug("OpenGLBackend: Initializing...")
        # Import OpenGL heavy modules lazily so environments without an OpenGL
        # stack (for example headless CI) can still import this module without
        # immediately failing.  ``is_available`` performs the necessary feature
        # probing before the backend is constructed, so any ImportError raised
        # here indicates a configuration drift between the probe and the
        # initialiser.  Surfacing the error keeps the log output actionable.
        try:
            # 稳定：不要再导入 QOpenGLExtraFunctions 和具体版本类
            from PySide6.QtOpenGL import QOpenGLVersionFunctionsFactory, QOpenGLVersionProfile
            from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
            from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram
            # --- END FIX ---
        except ImportError as exc:
            _LOGGER.error(
                "OpenGLBackend: Failed to import Qt OpenGL modules in __init__. "
                "This should have been caught by is_available(). Error: %s",
                exc,
                exc_info=True,
            )
            raise RuntimeError("Failed to import Qt OpenGL modules") from exc

        super().__init__()

        context, format_used = self._initialise_context(QOpenGLContext, QSurfaceFormat)
        self._context = context
        self._context_version = (
            format_used.majorVersion(),
            format_used.minorVersion(),
        )
        _LOGGER.info(
            "OpenGLBackend: Context initialised. Using Version: %d.%d",
            self._context_version[0],
            self._context_version[1],
        )

        self._surface: QOffscreenSurface = QOffscreenSurface()
        self._surface.setFormat(format_used)
        self._surface.create()
        if not self._surface.isValid():
            _LOGGER.error("OpenGLBackend: Failed to create valid QOffscreenSurface.")
            raise RuntimeError("OpenGL offscreen surface is invalid")
        _LOGGER.debug("OpenGLBackend: QOffscreenSurface created.")

        if not self._context.makeCurrent(self._surface):
            _LOGGER.error("OpenGLBackend: Failed to make OpenGL context current.")
            raise RuntimeError("Failed to make OpenGL context current")
        _LOGGER.debug("OpenGLBackend: Context made current.")

        functions = self._context.functions()
        # 某些版本返回的 QOpenGLFunctions 已经初始化；调用一次也安全
        try:
            functions.initializeOpenGLFunctions()
        except Exception as e:
            _LOGGER.error("OpenGLBackend: initializeOpenGLFunctions raised: %s", e, exc_info=True)
        self._gl = functions

        self._gl43 = None
        if self._context_version >= (4, 3):
            _LOGGER.debug("OpenGLBackend: Attempting to get GL 4.3 functions via VersionProfile.")
            prof = QOpenGLVersionProfile()
            prof.setVersion(4, 3)
            # 两种写法都行，任选其一：
            prof.setProfile(QSurfaceFormat.CoreProfile)
            gl43 = QOpenGLVersionFunctionsFactory.get(prof, self._context)
            if gl43 is not None:
                try:
                    gl43.initializeOpenGLFunctions()
                    self._gl43 = gl43
                    _LOGGER.info("OpenGLBackend: GL 4.3 functions initialized via VersionProfile.")
                except Exception:
                    _LOGGER.warning("OpenGLBackend: Failed to initialize GL 4.3 version functions.")
                    self._gl43 = None
            else:
                _LOGGER.warning("OpenGLBackend: GL 4.3 reported but functions not found (factory returned None).")
        else:
            _LOGGER.info("OpenGLBackend: GL version %s is less than 4.3, compute shader disabled.",
                         self._context_version)

        # Compile and link the shader program once.  The uniforms mirror the
        # tone-mapping helper in :mod:`iPhoto.core.image_filters` so both
        # backends stay in sync.  Compiling upfront amortises the cost across
        # all sessions, ensuring interactive slider tweaks remain snappy.
        self._program: QOpenGLShaderProgram = QOpenGLShaderProgram()
        vertex_shader = QOpenGLShader(QOpenGLShader.ShaderTypeBit.Vertex)
        _LOGGER.debug("OpenGLBackend: Compiling vertex shader...")
        if not vertex_shader.compileSourceCode(self._vertex_shader_source()):
            message = vertex_shader.log() or "unknown vertex shader error"
            _LOGGER.error("OpenGLBackend: Vertex shader compilation failed: %s", message)
            raise RuntimeError(f"Failed to compile OpenGL vertex shader: {message}")
        fragment_shader = QOpenGLShader(QOpenGLShader.ShaderTypeBit.Fragment)
        _LOGGER.debug("OpenGLBackend: Compiling fragment shader...")
        if not fragment_shader.compileSourceCode(self._fragment_shader_source()):
            message = fragment_shader.log() or "unknown fragment shader error"
            _LOGGER.error("OpenGLBackend: Fragment shader compilation failed: %s", message)
            raise RuntimeError(f"Failed to compile OpenGL fragment shader: {message}")
        self._program.addShader(vertex_shader)
        self._program.addShader(fragment_shader)
        _LOGGER.debug("OpenGLBackend: Linking shader program...")
        if not self._program.link():
            message = self._program.log() or "unknown shader link error"
            _LOGGER.error("OpenGLBackend: Shader program linking failed: %s", message)
            raise RuntimeError(f"Failed to link OpenGL shader program: {message}")
        _LOGGER.debug("OpenGLBackend: Shader program linked successfully.")

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
        _LOGGER.debug("OpenGLBackend: Uniform locations cached.")

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
            _LOGGER.error("OpenGLBackend: Failed to create OpenGL vertex buffer.")
            raise RuntimeError("Failed to create OpenGL vertex buffer")
        if not self._vertex_buffer.bind():
            _LOGGER.error("OpenGLBackend: Failed to bind OpenGL vertex buffer.")
            raise RuntimeError("Failed to bind OpenGL vertex buffer")
        raw_vertices = vertices.tobytes()
        self._vertex_buffer.allocate(raw_vertices, len(raw_vertices))
        self._vertex_buffer.release()
        _LOGGER.debug("OpenGLBackend: Vertex buffer created and allocated.")

        self._compute_program: QOpenGLShaderProgram | None = None
        self._stats_buffer: QOpenGLBuffer | None = None
        # 能力检测：必须有 GL4.3 函数，且 QOpenGLBuffer.Type 暴露了 ShaderStorageBuffer
        _has_ssbo = False
        try:
            from PySide6.QtOpenGL import QOpenGLBuffer as _QBuf
            _has_ssbo = hasattr(_QBuf.Type, "ShaderStorageBuffer")
        except Exception:
            _has_ssbo = False

        if self._gl43 is not None and _has_ssbo:
            try:
                self._compute_program = self._compile_compute_shader()
                if self._compute_program is not None:
                    self._stats_buffer = _QBuf(_QBuf.Type.ShaderStorageBuffer)
                    if not self._stats_buffer.create():
                        _LOGGER.warning("OpenGLBackend: Failed to create stats buffer (SSBO). Disabling compute stats.")
                        self._stats_buffer = None
                        self._compute_program = None
                    else:
                        _LOGGER.info("OpenGLBackend: Compute shader and stats buffer created successfully.")
            except Exception as exc:
                _LOGGER.warning(
                    "OpenGLBackend: Failed to enable compute stats: %s. Falling back to CPU statistics.",
                    exc, exc_info=True
                )
                self._compute_program = None
                self._stats_buffer = None
        else:
            _LOGGER.info("OpenGLBackend: SSBO/GL4.3 not available in this build, compute stats disabled.")

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

        _LOGGER.debug("OpenGLBackend: Attempting to compile compute shader...")
        if self._gl43 is None:
            _LOGGER.info(
                "OpenGLBackend: GL 4.3 functions not available. Cannot compile compute shader."
            )
            return None

        from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram

        program = QOpenGLShaderProgram()
        shader = QOpenGLShader(QOpenGLShader.ShaderTypeBit.Compute)
        if not shader.compileSourceCode(self._compute_shader_source()):
            message = shader.log() or "unknown compute shader error"
            _LOGGER.error("OpenGLBackend: Compute shader compilation failed: %s", message)
            raise RuntimeError(f"Failed to compile OpenGL compute shader: {message}")
        program.addShader(shader)
        _LOGGER.debug("OpenGLBackend: Linking compute shader program...")
        if not program.link():
            message = program.log() or "unknown compute shader link error"
            _LOGGER.error(
                "OpenGLBackend: Compute shader program linking failed: %s", message
            )
            raise RuntimeError(f"Failed to link OpenGL compute shader program: {message}")
        _LOGGER.info("OpenGLBackend: Compute shader compiled and linked successfully.")
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
        _LOGGER.debug("OpenGL: Checking availability...")
        try:
            from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
        except Exception as exc:
            _LOGGER.warning("OpenGL: Availability check failed. Cannot import QtGui OpenGL pieces: %s",
                            exc, exc_info=True)
            return False

        try:
            from PySide6.QtOpenGLWidgets import QOpenGLWidget  # 需要 Addons
            from PySide6 import QtOpenGL as _qt_opengl  # 模块存在即可
        except Exception as e:
            _LOGGER.error("OpenGL availability false: %s", e)
            return False

        # 若已有全局 share context，直接相信它
        share_context = QOpenGLContext.globalShareContext()
        if share_context is not None and share_context.isValid():
            share_format = share_context.format()
            if (share_format.renderableType() == QSurfaceFormat.RenderableType.OpenGL
                    and share_format.majorVersion() >= 3):
                _LOGGER.info(
                    "OpenGL: Availability check: Found valid global share context. "
                    "Version %d.%d. Assuming OpenGL is available.",
                    share_format.majorVersion(),
                    share_format.minorVersion(),
                )
                return True

        _LOGGER.debug("OpenGL: Availability check: No valid global share context found, proceeding with manual check.")

        context: QOpenGLContext | None = None
        surface: QOffscreenSurface | None = None
        try:
            _LOGGER.debug("OpenGL: Availability check: Initialising context...")
            context, format_hint = cls._initialise_context(QOpenGLContext, QSurfaceFormat)
            _LOGGER.debug("OpenGL: Availability check: Context initialised.")

            surface = QOffscreenSurface()
            surface.setFormat(format_hint)
            surface.create()
            if not surface.isValid():
                _LOGGER.warning("OpenGL: Availability check: Offscreen surface is invalid.")
                return False

            _LOGGER.info("OpenGL: Availability check: Context and surface creation successful. Assuming available.")
            return True

        except Exception as exc:
            _LOGGER.warning("OpenGL: Availability check failed with exception: %s", exc, exc_info=True)
            return False
        finally:
            if context is not None:
                try:
                    context.deleteLater()
                except Exception:
                    pass
            if surface is not None:
                try:
                    surface.destroy()
                except Exception:
                    pass

    def _make_current(self) -> bool:
        _LOGGER.debug("OpenGLBackend: Attempting to make context current...")
        try:
            if not self._context.makeCurrent(self._surface):
                _LOGGER.warning("OpenGLBackend: makeCurrent failed.")
                return False
            _LOGGER.debug("OpenGLBackend: makeCurrent successful.")

            # 每次切换当前上下文后，都重新抓一次函数表
            funcs = self._context.functions()
            try:
                funcs.initializeOpenGLFunctions()  # 有的实现返回 None，不要用返回值做判据
            except Exception as e:
                _LOGGER.warning("OpenGLBackend: initializeOpenGLFunctions raised: %s", e)

            # 功能性校验：至少得有 glGenTextures
            if not hasattr(funcs, "glGenTextures"):
                _LOGGER.error("OpenGLBackend: QOpenGLFunctions missing glGenTextures after makeCurrent.")
                self._context.doneCurrent()
                return False

            self._gl = funcs
            return True

        except Exception as exc:
            _LOGGER.error("OpenGLBackend: makeCurrent raised exception: %s", exc, exc_info=True)
            return False


    def create_session(self, image: QImage) -> PreviewSession:
        _LOGGER.debug("OpenGLBackend: Creating session...")
        from PySide6.QtGui import QImage as QtImage
        from PySide6.QtOpenGL import QOpenGLFramebufferObject

        if image.isNull():
            _LOGGER.debug("OpenGLBackend: Image is null, creating empty session.")
            # Return a lightweight placeholder session so callers can proceed
            # without handling a special case.  Rendering an empty session
            # results in a null image which mirrors the CPU backend's
            # behaviour when asked to process an invalid ``QImage``.
            return _OpenGlPreviewSession(0, 0, 0, None, ColorStats())

        if not self._make_current():
            _LOGGER.error(
                "OpenGLBackend: Failed to activate context for session creation."
            )
            raise RuntimeError("Failed to activate OpenGL context for session creation")

        converted = image.convertToFormat(QtImage.Format.Format_RGBA8888)
        width = converted.width()
        height = converted.height()
        _LOGGER.debug("OpenGLBackend: Image converted to RGBA8888 (%dx%d).", width, height)

        texture_id = self._generate_texture()
        _LOGGER.debug("OpenGLBackend: Generated texture ID: %d", texture_id)
        self._upload_texture(texture_id, converted)
        _LOGGER.debug("OpenGLBackend: Texture uploaded.")

        framebuffer = QOpenGLFramebufferObject(width, height)
        if not framebuffer.isValid():
            _LOGGER.error("OpenGLBackend: Failed to create valid FBO.")
            # Clean up texture
            texture_ids = (ctypes.c_uint * 1)(texture_id)
            self._gl.glDeleteTextures(1, texture_ids)
            self._context.doneCurrent()
            raise RuntimeError("Failed to create OpenGL Framebuffer Object")

        _LOGGER.debug("OpenGLBackend: FBO created.")

        stats = self._compute_session_stats(texture_id, width, height, converted)
        _LOGGER.debug("OpenGLBackend: Color stats computed.")

        self._context.doneCurrent()
        _LOGGER.debug("OpenGLBackend: Session creation complete, context released.")

        return _OpenGlPreviewSession(width, height, texture_id, framebuffer, stats)

    def _generate_texture(self) -> int:
        """Create and return a new OpenGL texture identifier."""
        texture_ids = (ctypes.c_uint * 1)()
        self._gl.glGenTextures(1, texture_ids)
        tex = int(texture_ids[0])
        if tex == 0:
            _LOGGER.warning("OpenGLBackend: glGenTextures returned 0, retrying after reinit functions...")
            # 试着重新初始化函数表（万一 makeCurrent 后被其他调用改了状态）
            try:
                funcs = self._context.functions()
                if funcs.initializeOpenGLFunctions():
                    self._gl = funcs
                    self._gl.glGenTextures(1, texture_ids)
                    tex = int(texture_ids[0])
            except Exception as e:
                _LOGGER.error("OpenGLBackend: reinit functions failed: %s", e, exc_info=True)
            if tex == 0:
                # 最终失败，尽量打印错误代码辅助定位
                try:
                    err = self._gl.glGetError()
                    _LOGGER.error("OpenGLBackend: glGenTextures still 0, glGetError=0x%X", err)
                except Exception:
                    pass
        return tex

    def _upload_texture(self, texture_id: int, image: QImage) -> None:
        """Upload *image* data to the GPU texture identified by *texture_id*."""
        _LOGGER.debug("OpenGLBackend: Uploading texture for ID %d...", texture_id)
        if texture_id == 0:
            _LOGGER.error("OpenGLBackend: Invalid texture ID 0 for upload.")
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
        _LOGGER.debug("OpenGLBackend: Texture upload complete for ID %d.", texture_id)

    def _compute_session_stats(
        self,
        texture_id: int,
        width: int,
        height: int,
        fallback_image: QImage,
    ) -> ColorStats:
        """Return :class:`ColorStats` using the compute shader when available."""
        _LOGGER.debug("OpenGLBackend: Computing session stats...")
        fallback_stats: ColorStats | None = None

        def _fallback(reason: str) -> ColorStats:
            nonlocal fallback_stats
            _LOGGER.warning("OpenGLBackend: %s. Falling back to CPU stats.", reason)
            if fallback_stats is None:
                _LOGGER.debug("OpenGLBackend: Calculating CPU stats now.")
                fallback_stats = compute_color_statistics(fallback_image)
            return fallback_stats

        if texture_id == 0:
            return _fallback("Texture ID is 0")
        if width == 0 or height == 0:
            return _fallback(f"Invalid dimensions (width={width}, height={height})")
        if self._compute_program is None:
            return _fallback("Compute program not available")
        if self._stats_buffer is None:
            return _fallback("Stats buffer not available")
        if self._gl43 is None:
            return _fallback("GL 4.3 not available")

        _LOGGER.debug("OpenGLBackend: Using compute shader for stats.")
        groups_x = (width + 15) // 16
        groups_y = (height + 15) // 16
        group_count = max(groups_x * groups_y, 1)
        stride = 320
        total_size = stride * group_count
        _LOGGER.debug(
            "OpenGLBackend: Compute dispatch groups: %dx%d (%d total)",
            groups_x,
            groups_y,
            group_count,
        )

        if not self._stats_buffer.bind():
            return _fallback("Failed to bind stats buffer")
        # Allocate or resize the buffer to hold one record per work group.
        try:
            self._stats_buffer.allocate(total_size)
        except Exception as exc:
            _LOGGER.error(
                "OpenGLBackend: Failed to allocate stats buffer (size %d): %s",
                total_size,
                exc,
                exc_info=True,
            )
            self._stats_buffer.release()
            return _fallback("Failed to allocate stats buffer")
        self._stats_buffer.release()
        _LOGGER.debug("OpenGLBackend: Stats buffer allocated (size %d).", total_size)

        gl = self._gl
        gl43 = self._gl43
        program = self._compute_program
        assert program is not None  # guarded above

        if not program.bind():
            return _fallback("Failed to bind compute program")
        _LOGGER.debug("OpenGLBackend: Compute program bound.")

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)
        program.setUniformValue("uTex", 0)

        buffer_id = self._stats_buffer.bufferId()
        gl43.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 1, buffer_id)
        _LOGGER.debug("OpenGLBackend: Dispatching compute shader...")
        gl43.glDispatchCompute(groups_x, groups_y, 1)
        gl43.glMemoryBarrier(gl.GL_SHADER_STORAGE_BARRIER_BIT | gl.GL_BUFFER_UPDATE_BARRIER_BIT)
        _LOGGER.debug("OpenGLBackend: Compute dispatch complete.")

        program.release()
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

        if not self._stats_buffer.bind():
            return _fallback("Failed to bind stats buffer for reading")

        _LOGGER.debug("OpenGLBackend: Mapping stats buffer for read...")
        mapped = self._stats_buffer.mapRange(
            0,
            total_size,
            QOpenGLBuffer.RangeAccessFlag.ReadAccess,
        )
        if mapped is None:
            self._stats_buffer.release()
            return _fallback("Failed to map stats buffer")
        _LOGGER.debug("OpenGLBackend: Stats buffer mapped.")

        raw = bytes(mapped)
        self._stats_buffer.unmap()
        self._stats_buffer.release()

        # Some drivers align SSBO records beyond the declared payload.  Derive the
        # actual stride from the returned data to ensure we walk the buffer
        # correctly on all platforms.
        if len(raw) < total_size:
            _LOGGER.warning(
                "OpenGLBackend: Mapped buffer size (%d) is less than expected (%d).",
                len(raw),
                total_size,
            )
            return _fallback("Mapped buffer size mismatch")

        actual_stride = len(raw) // group_count
        if actual_stride > stride:
            _LOGGER.warning(
                "OpenGLBackend: Detected driver SSBO stride %d (expected %d).",
                actual_stride,
                stride,
            )
            stride = actual_stride
        elif actual_stride < stride:
            _LOGGER.error(
                "OpenGLBackend: Mapped buffer stride (%d) is smaller than expected (%d).",
                actual_stride,
                stride,
            )
            return _fallback("Mapped buffer stride mismatch")

        sum_saturation = 0.0
        sum_lin_r = 0.0
        sum_lin_g = 0.0
        sum_lin_b = 0.0
        count = 0
        highlight_count = 0
        dark_count = 0
        skin_count = 0
        histogram = [0] * 64

        _LOGGER.debug("OpenGLBackend: Aggregating stats from %d groups...", group_count)
        for index in range(group_count):
            base = index * stride
            if base + 288 > len(raw):
                _LOGGER.error(
                    "OpenGLBackend: Buffer overrun while reading group %d. Stopping aggregation.",
                    index,
                )
                break
            try:
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
            except struct.error as exc:
                _LOGGER.error(
                    "OpenGLBackend: Struct unpack failed at group %d (base=%d): %s",
                    index,
                    base,
                    exc,
                )
                return _fallback("Failed to parse stats buffer")

        _LOGGER.debug("OpenGLBackend: Aggregation complete. Total pixels: %d", count)
        if count == 0:
            _LOGGER.warning("OpenGLBackend: Compute stats returned 0 pixel count.")
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

        stats = ColorStats(
            saturation_mean=min(max(mean_saturation, 0.0), 1.0),
            saturation_median=min(max(median_saturation, 0.0), 1.0),
            highlight_ratio=min(max(highlight_ratio, 0.0), 1.0),
            dark_ratio=min(max(dark_ratio, 0.0), 1.0),
            skin_ratio=min(max(skin_ratio, 0.0), 1.0),
            cast_magnitude=min(max(cast_magnitude, 0.0), 1.0),
            white_balance_gain=(gain_r, gain_g, gain_b),
        )
        _LOGGER.debug("OpenGLBackend: Compute stats calculation complete: %s", stats)
        return stats

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:
        _LOGGER.debug("OpenGLBackend: Rendering...")
        from PySide6.QtGui import QImage as QtImage

        gl_session = cast(_OpenGlPreviewSession, session)
        if gl_session.width == 0 or gl_session.height == 0:
            _LOGGER.debug("OpenGLBackend: Empty session, returning null image.")
            return QImage()

        if not self._make_current():
            _LOGGER.error("OpenGLBackend: Failed to activate context for rendering.")
            raise RuntimeError("Failed to activate OpenGL context for preview rendering")

        gl = self._gl
        framebuffer = gl_session.framebuffer
        if framebuffer is None:
            _LOGGER.error("OpenGLBackend: Session framebuffer is None.")
            self._context.doneCurrent()
            return QImage()

        if not framebuffer.bind():
            _LOGGER.error("OpenGLBackend: Failed to bind FBO for rendering.")
            self._context.doneCurrent()
            raise RuntimeError("Failed to bind OpenGL framebuffer")

        gl.glViewport(0, 0, gl_session.width, gl_session.height)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)

        program = self._program
        if not program.bind():
            _LOGGER.error("OpenGLBackend: Failed to bind shader program for rendering.")
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
            _LOGGER.error(
                "OpenGLBackend: Failed to bind vertex buffer for rendering."
            )
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

        _LOGGER.debug("OpenGLBackend: Drawing arrays...")
        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)

        program.disableAttributeArray(self._position_location)
        program.disableAttributeArray(self._texcoord_location)
        self._vertex_buffer.release()
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        program.release()
        _LOGGER.debug("OpenGLBackend: Draw complete.")

        image = framebuffer.toImage(True).convertToFormat(QtImage.Format.Format_ARGB32)
        framebuffer.release()
        _LOGGER.debug("OpenGLBackend: Image read back from FBO.")

        self._context.doneCurrent()
        _LOGGER.debug("OpenGLBackend: Render complete, context released.")

        return image

    def dispose_session(self, session: PreviewSession) -> None:
        _LOGGER.debug("OpenGLBackend: Disposing session...")
        gl_session = cast(_OpenGlPreviewSession, session)

        if gl_session.texture_id == 0 and gl_session.framebuffer is None:
            _LOGGER.debug("OpenGLBackend: Session already empty, nothing to dispose.")
            return

        if self._make_current():
            _LOGGER.debug("OpenGLBackend: Context made current for disposal.")
            if gl_session.texture_id != 0:
                _LOGGER.debug(
                    "OpenGLBackend: Deleting texture ID: %d", gl_session.texture_id
                )
                texture_ids = (ctypes.c_uint * 1)(gl_session.texture_id)
                self._gl.glDeleteTextures(1, texture_ids)
                gl_session.texture_id = 0
            framebuffer = gl_session.framebuffer
            if framebuffer is not None:
                _LOGGER.debug("OpenGLBackend: Deleting FBO.")
                # ``QOpenGLFramebufferObject`` releases its resources when the
                # Python wrapper is destroyed.  Clearing our reference allows Qt
                # to free the underlying OpenGL object while the context is
                # still current.
                if framebuffer.isBound():
                    framebuffer.release()
                gl_session.framebuffer = None
                del framebuffer
            self._context.doneCurrent()
            _LOGGER.debug("OpenGLBackend: Disposal complete, context released.")
        else:
            _LOGGER.warning(
                "OpenGLBackend: Could not make context current to dispose session. Resources may leak."
            )

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
        _LOGGER.debug("Disposing _OpenGlPreviewSession (clearing references).")
        self.framebuffer = None
        self.texture_id = 0


def select_preview_backend() -> PreviewBackend:
    """Return the most capable preview backend available on the system."""
    _LOGGER.debug("Selecting preview backend (called by select_preview_backend)...")

    # CUDA backend has the highest priority.
    _LOGGER.debug("Checking for CUDA backend (called by select_preview_backend)...")
    if _CudaPreviewBackend.is_available():
        try:
            backend = _CudaPreviewBackend()
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning(
                "Failed to initialise CUDA backend: %s. Called by select_preview_backend.",
                exc,
                exc_info=True,
            )
        else:
            _LOGGER.info("Using CUDA preview backend (selected by select_preview_backend).")
            return backend
    else:
        _LOGGER.debug("CUDA backend not available (checked by select_preview_backend).")

    # OpenGL is the next best choice when CUDA is not available.
    _LOGGER.debug("Checking for OpenGL backend (called by select_preview_backend)...")
    if _OpenGlPreviewBackend.is_available():
        try:
            backend = _OpenGlPreviewBackend()
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning(
                "Failed to initialise OpenGL backend: %s. Called by select_preview_backend.",
                exc,
                exc_info=True,
            )
        else:
            _LOGGER.info(
                "Using OpenGL preview backend (selected by select_preview_backend)."
            )
            return backend
    else:
        _LOGGER.warning(
            "OpenGL backend not available (checked by select_preview_backend)."
        )

    backend = _CpuPreviewBackend()
    _LOGGER.info(
        "Falling back to CPU preview backend (by select_preview_backend)."
    )
    return backend


def fallback_preview_backend(previous: PreviewBackend) -> PreviewBackend:
    """Return a safer backend after *previous* reports a fatal failure."""
    _LOGGER.warning(
        "Falling back from previous backend: %s (called by fallback_preview_backend).",
        previous.tier_name,
    )

    # ``_CudaPreviewBackend`` currently raises during construction but the
    # ``isinstance`` guard keeps the helper forward-compatible for future
    # implementations.  Prefer stepping down one tier at a time so the caller
    # retains hardware acceleration whenever possible.
    if isinstance(previous, _CudaPreviewBackend):  # pragma: no cover - defensive
        _LOGGER.debug(
            "Previous backend was CUDA. Trying OpenGL... (by fallback_preview_backend)"
        )
        if _OpenGlPreviewBackend.is_available():
            try:
                backend = _OpenGlPreviewBackend()
            except Exception as exc:
                _LOGGER.error(
                    "Failed to initialise OpenGL as fallback from CUDA: %s. "
                    "Falling back to CPU. (by fallback_preview_backend)",
                    exc,
                    exc_info=True,
                )
            else:
                _LOGGER.info(
                    "Falling back from CUDA preview backend to OpenGL implementation "
                    "(by fallback_preview_backend)."
                )
                return backend
        else:
            _LOGGER.warning(
                "OpenGL not available as fallback from CUDA. "
                "Falling back to CPU. (by fallback_preview_backend)"
            )

    if isinstance(previous, _OpenGlPreviewBackend):
        _LOGGER.info(
            "Falling back from OpenGL preview backend to CPU implementation "
            "(by fallback_preview_backend)."
        )
        return _CpuPreviewBackend()

    # Any other backend (including the CPU fallback) drops straight to the
    # baseline CPU implementation so callers always receive a usable renderer.
    _LOGGER.info(
        "Defaulting to CPU implementation (by fallback_preview_backend)."
    )
    return _CpuPreviewBackend()


__all__ = [
    "PreviewBackend",
    "PreviewSession",
    "fallback_preview_backend",
    "select_preview_backend",
]