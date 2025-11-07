# -*- coding: utf-8 -*-
"""
GPU-accelerated image viewer (pure OpenGL texture upload; pixel-accurate zoom/pan).
- Ensures magnification samples the ORIGINAL pixels (no Qt/FBO resampling).
- Uses GL 3.3 Core, VAO/VBO, and a raw glTexImage2D + glTexSubImage2D upload path.
"""

from __future__ import annotations

import ctypes
import numpy as np
from typing import Mapping, Optional

import logging

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QWheelEvent,
    QSurfaceFormat,
    QOpenGLContext,
    QPixmap,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFramebufferObject,
    QOpenGLFramebufferObjectFormat,
    QOpenGLFunctions_3_3_Core,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLVertexArrayObject, QOpenGLDebugLogger,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel
from OpenGL import GL as gl


_LOGGER = logging.getLogger(__name__)

# 如果你的工程没有这个函数，可以改成固定背景色
try:
    from ..palette import viewer_surface_color  # type: ignore
except Exception:
    def viewer_surface_color(_):  # fallback
        return QColor(0, 0, 0)

# ==================== GLSL（像素精确采样） ====================

VERTEX_SHADER = r"""
#version 330 core
layout (location = 0) in vec2 aPos;  // NDC [-1,1]^2
out vec2 v_ndc;
void main() {
    v_ndc = aPos;
    gl_Position = vec4(aPos, 0.0, 1.0);
}
"""

FRAGMENT_SHADER = r"""
#version 330 core
in vec2 v_ndc;
out vec4 FragColor;

uniform sampler2D uTex;     // 原图纹理
uniform vec2 uTexSize;      // 原图像素尺寸 (W,H)
uniform vec2 uViewSize;     // 视口像素尺寸 (W,H)，考虑 devicePixelRatio
uniform float uZoom;        // 放大倍数（>1 放大）
uniform vec2 uPanPx;        // 以“视口像素”为单位的平移（右+X，下+Y）

// 对齐像素中心，避免半像素偏移
vec2 pixelCenterUV(vec2 fragPx) {
    // 屏幕像素 -> 纹理像素，再除以纹理尺寸得到 uv
    vec2 texel = (fragPx - uPanPx) / uZoom + 0.5;
    return texel / uTexSize;
}

void main() {
    // gl_FragCoord 是 1-based 的屏幕像素坐标（含 DPR 后的实际像素）
    vec2 fragPx = vec2(gl_FragCoord.x - 1.0, gl_FragCoord.y - 1.0);

    vec2 uv = pixelCenterUV(fragPx);

    // 越界裁剪（也可改成边缘颜色）
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) {
        discard;
    }

    // 放大采用 NEAREST，可看到原始像素；缩小可改 LINEAR 但不在 shader 内控制
    vec4 c = texture(uTex, uv);
    FragColor = c;
}
"""


class GLImageViewer(QOpenGLWidget):
    """A QWidget that displays GPU-rendered images with pixel-accurate zoom."""

    # Signals（保持与旧版一致）
    replayRequested = Signal()
    zoomChanged = Signal(float)
    nextItemRequested = Signal()
    prevItemRequested = Signal()
    fullscreenExitRequested = Signal()
    fullscreenToggleRequested = Signal()

    def __init__(self, parent: Optional["QOpenGLWidget"] = None) -> None:
        super().__init__(parent)

        # 强制 3.3 Core
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)
        self._gl_funcs = None
        self._gl_extra = None

        # 状态
        self._image: Optional[QImage] = None
        self._adjustments: dict[str, float] = {}
        self._pending_adjustments: Optional[dict[str, float]] = None
        self._current_image_source: Optional[object] = None
        self._zoom_factor: float = 1.0
        self._min_zoom: float = 0.1
        self._max_zoom: float = 16.0
        # Store the pan offset in physical viewport pixels relative to the centred
        # image so we can translate the rendered texture the same way the legacy
        # ``ImageViewer`` scrolled its pixmap.
        self._pan_px: QPointF = QPointF(0.0, 0.0)
        self._is_panning: bool = False
        self._pan_start_pos: QPointF = QPointF()
        self._wheel_action: str = "zoom"  # 或 "navigate"
        self._live_replay_enabled: bool = False

        # Track the viewer surface colour so immersive mode can temporarily
        # switch to a pure black canvas.  ``viewer_surface_color`` returns a
        # palette-derived colour string, which we normalise to ``QColor`` for
        # reliable comparisons and GL clear colour conversion.
        self._default_surface_color = self._normalise_colour(viewer_surface_color(self))
        self._surface_override: Optional[QColor] = None
        self._backdrop_color: QColor = QColor(self._default_surface_color)
        self._apply_surface_color()

        self._loading_overlay = QLabel("Loading…", self)
        self._loading_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self._loading_overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 128); color: white; font-size: 18px;"
        )
        self._loading_overlay.hide()

        # 原生纹理（绕过 QOpenGLTexture；确保原图像素）
        self._tex_id: int = 0
        self._tex_w: int = 0
        self._tex_h: int = 0

        # GL objs
        self._gl_funcs: Optional[QOpenGLFunctions_3_3_Core] = None
        self._shader_program: Optional[QOpenGLShaderProgram] = None
        self._vao: Optional[QOpenGLVertexArrayObject] = None
        self._vbo: Optional[QOpenGLBuffer] = None
        self._uni: dict[str, int] = {}

        self.reset_zoom()

    # --------------------------- Public API ---------------------------

    def shutdown(self) -> None:
        """Clean up GL resources."""
        self.makeCurrent()
        try:
            self._delete_raw_texture()
            if self._vbo:
                self._vbo.destroy()
                self._vbo = None
            if self._vao:
                self._vao.destroy()
                self._vao = None
            if self._shader_program:
                self._shader_program.removeAllShaders()
                self._shader_program = None
        finally:
            self.doneCurrent()

    def set_image(
        self,
        image: Optional[QImage],
        adjustments: Optional[Mapping[str, float]] = None,
        *,
        image_source: Optional[object] = None,
        reset_view: bool = True,
    ) -> None:
        """Display *image* together with optional colour *adjustments*.

        Parameters
        ----------
        image:
            ``QImage`` backing the GL texture. ``None`` clears the viewer.
        adjustments:
            Mapping of Photos-style adjustment values to apply in the shader.
        image_source:
            Stable identifier describing where *image* originated.  When the
            identifier matches the one from the previous call the viewer keeps
            the existing GPU texture, avoiding redundant uploads during view
            transitions.
        reset_view:
            ``True`` preserves the historic behaviour of resetting the zoom and
            pan state.  Passing ``False`` keeps the current transform so edit
            mode can reuse the detail view framing without a visible jump.
        """

        reuse_existing_texture = (
            image_source is not None and image_source == getattr(self, "_current_image_source", None)
        )

        if reuse_existing_texture and image is not None and not image.isNull():
            # Skip the heavy texture re-upload when the caller explicitly
            # reports that the source asset is unchanged.  Only the adjustment
            # uniforms need to be refreshed in this scenario.
            self.set_adjustments(adjustments)
            if reset_view:
                self.reset_zoom()
            return

        self._current_image_source = image_source
        self._image = image
        mapped_adjustments = dict(adjustments or {})
        self._pending_adjustments = mapped_adjustments
        self._adjustments = mapped_adjustments
        self._loading_overlay.hide()

        if image is None or image.isNull():
            self._current_image_source = None
            # Releasing GL textures requires a current context.  ``makeCurrent``
            # safely becomes a no-op until the widget is shown, so we can call
            # it even when the viewer is still hidden during start-up.
            if self.context() is not None:
                self.makeCurrent()
                try:
                    self._delete_raw_texture()
                finally:
                    self.doneCurrent()
            else:
                self._tex_id = 0
                self._tex_w = self._tex_h = 0

        if reset_view:
            # Reset the interactive transform so every new asset begins in the
            # same fit-to-window baseline that the QWidget-based viewer
            # exposes.  ``reset_view`` lets callers preserve the zoom when the
            # user toggles between detail and edit modes.
            self.reset_zoom()
    def set_placeholder(self, pixmap) -> None:
        """Display *pixmap* without changing the tracked image source."""

        if pixmap and not pixmap.isNull():
            self.set_image(pixmap.toImage(), {}, image_source=self._current_image_source)
        else:
            self.set_image(None, {}, image_source=None)

    def set_pixmap(
        self,
        pixmap: Optional[QPixmap],
        image_source: Optional[object] = None,
        *,
        reset_view: bool = True,
    ) -> None:
        """Compatibility wrapper mirroring :class:`ImageViewer`.

        The optional *image_source* is forwarded to :meth:`set_image` so callers
        can keep the existing texture alive when reusing the same asset.
        """

        if pixmap is None or pixmap.isNull():
            self.set_image(None, {}, image_source=None, reset_view=reset_view)
            return
        self.set_image(
            pixmap.toImage(),
            {},
            image_source=image_source if image_source is not None else self._current_image_source,
            reset_view=reset_view,
        )

    def clear(self) -> None:
        """Reset the viewer to an empty state."""

        self.set_image(None, {}, image_source=None)

    def set_adjustments(self, adjustments: Optional[Mapping[str, float]] = None) -> None:
        """Update the active adjustment uniforms without replacing the texture."""

        mapped_adjustments = dict(adjustments or {})
        self._pending_adjustments = mapped_adjustments
        self._adjustments = mapped_adjustments
        self.update()

    def current_image_source(self) -> Optional[object]:
        """Return the identifier describing the currently displayed image."""

        return getattr(self, "_current_image_source", None)

    def pixmap(self) -> Optional[QPixmap]:
        """Return a defensive copy of the currently displayed frame."""

        if self._image is None or self._image.isNull():
            return None
        return QPixmap.fromImage(self._image)

    def set_loading(self, loading: bool) -> None:
        """Toggle the translucent loading overlay."""

        if loading:
            self._loading_overlay.setVisible(True)
            self._loading_overlay.raise_()
            self._loading_overlay.resize(self.size())
        else:
            self._loading_overlay.hide()

    def viewport_widget(self) -> "GLImageViewer":
        """Expose the drawable widget for API parity with :class:`ImageViewer`."""

        return self

    def set_live_replay_enabled(self, enabled: bool) -> None:
        self._live_replay_enabled = bool(enabled)

    def set_wheel_action(self, action: str) -> None:
        self._wheel_action = "zoom" if action == "zoom" else "navigate"

    def set_surface_color_override(self, colour: str | None) -> None:
        """Override the viewer backdrop with *colour* or restore the default."""

        if colour is None:
            self._surface_override = None
        else:
            self._surface_override = self._normalise_colour(colour)
        self._apply_surface_color()

    def set_immersive_background(self, immersive: bool) -> None:
        """Toggle the pure black immersive backdrop used in immersive mode."""

        self.set_surface_color_override("#000000" if immersive else None)

    def set_zoom(self, factor: float, anchor: Optional[QPointF] = None) -> None:
        """Adjust the zoom while preserving the requested *anchor* pixel."""

        clamped = max(self._min_zoom, min(self._max_zoom, float(factor)))
        if abs(clamped - self._zoom_factor) < 1e-6:
            return

        # Default to the viewport centre so toolbar actions mirror the behaviour of
        # the QWidget-based ``ImageViewer`` when no explicit anchor is supplied.
        anchor_point = anchor or self.viewport_center()

        if (
            anchor_point is not None
            and self._tex_w > 0
            and self._tex_h > 0
            and self.width() > 0
            and self.height() > 0
        ):
            dpr = self.devicePixelRatioF()
            view_width = float(self.width()) * dpr
            view_height = float(self.height()) * dpr
            base_scale = self._fit_to_view_scale(view_width, view_height)
            old_scale = base_scale * self._zoom_factor
            new_scale = base_scale * clamped
            if old_scale > 1e-6 and new_scale > 0.0:
                # Convert the Qt-provided anchor (origin top-left) into a
                # bottom-left coordinate system so we can reuse the OpenGL
                # convention when solving for the new pan offset.
                anchor_bottom_left = QPointF(
                    anchor_point.x() * dpr,
                    view_height - anchor_point.y() * dpr,
                )
                view_centre = QPointF(view_width / 2.0, view_height / 2.0)
                anchor_vector = anchor_bottom_left - view_centre

                # Solve ``v = scale * t + pan`` for the pan term that keeps the
                # texture coordinate ``t`` mapped to the same on-screen pixel ``v``
                # after the zoom factor changes.
                current_pan = self._pan_px
                tex_coord_x = (anchor_vector.x() - current_pan.x()) / old_scale
                tex_coord_y = (anchor_vector.y() - current_pan.y()) / old_scale
                self._pan_px = QPointF(
                    anchor_vector.x() - tex_coord_x * new_scale,
                    anchor_vector.y() - tex_coord_y * new_scale,
                )

        self._zoom_factor = clamped
        self.update()
        self.zoomChanged.emit(self._zoom_factor)

    def reset_zoom(self) -> None:
        self._zoom_factor = 1.0
        self._pan_px = QPointF(0.0, 0.0)
        self.update()
        self.zoomChanged.emit(self._zoom_factor)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom_factor * 1.1, anchor=self.viewport_center())

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom_factor / 1.1, anchor=self.viewport_center())

    def viewport_center(self) -> QPointF:
        return QPointF(self.width() / 2, self.height() / 2)

    # --------------------------- Off-screen rendering ---------------------------

    def render_offscreen_image(
        self,
        target_size: QSize,
        adjustments: Optional[Mapping[str, float]] = None,
    ) -> QImage:
        """Render the current texture into an off-screen framebuffer.

        Parameters
        ----------
        target_size:
            Final size of the rendered preview.  The method clamps the width
            and height to at least one pixel to avoid driver errors caused by
            zero-sized viewports.
        adjustments:
            Mapping of shader uniform values to apply during rendering.  Passing
            ``None`` renders the frame using the viewer's current adjustment
            state.

        Returns
        -------
        QImage
            CPU-side image containing the rendered frame.  The image is always
            converted to ``Format_ARGB32`` so downstream consumers can compute
            statistics without needing to normalise the pixel layout first.
        """

        if target_size.isEmpty():
            _LOGGER.warning("render_offscreen_image: target size was empty")
            return QImage()

        context = self.context()
        if context is None:
            _LOGGER.warning("render_offscreen_image: no OpenGL context available")
            return QImage()

        if self._image is None or self._image.isNull():
            _LOGGER.warning("render_offscreen_image: no source image bound to the viewer")
            return QImage()

        # Ensure we have a usable texture before issuing draw commands.  The
        # upload path mirrors the guard inside :meth:`paintGL` so repeated calls
        # remain cheap once the texture is resident on the GPU.
        self.makeCurrent()
        try:
            if self._tex_id == 0:
                self._upload_texture_raw_gl(self._image)
            if self._tex_id == 0:
                _LOGGER.error("render_offscreen_image: texture upload failed")
                return QImage()

            gf = self._gl_funcs
            if gf is None:
                gf = QOpenGLFunctions_3_3_Core()
                gf.initializeOpenGLFunctions()
                self._gl_funcs = gf

            width = max(1, int(target_size.width()))
            height = max(1, int(target_size.height()))

            # Preserve the caller's framebuffer binding and viewport so invoking
            # this helper does not disturb the widget's onscreen presentation.
            previous_fbo = gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING)
            previous_viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)

            fbo_format = QOpenGLFramebufferObjectFormat()
            fbo_format.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
            fbo_format.setTextureTarget(gl.GL_TEXTURE_2D)
            fbo = QOpenGLFramebufferObject(width, height, fbo_format)
            if not fbo.isValid():
                _LOGGER.error("render_offscreen_image: failed to allocate framebuffer object")
                return QImage()

            try:
                fbo.bind()
                gf.glViewport(0, 0, width, height)
                gf.glClearColor(0.0, 0.0, 0.0, 0.0)
                gf.glClear(gl.GL_COLOR_BUFFER_BIT)

                prog = self._shader_program
                if prog is None or not prog.bind():
                    _LOGGER.error("render_offscreen_image: shader program was not available")
                    return QImage()

                try:
                    if self._dummy_vao:
                        self._dummy_vao.bind()

                    # Apply adjustment uniforms using the same helpers as the onscreen path.
                    adj = dict(adjustments or self._adjustments)

                    def uniform_location(name: str) -> int:
                        return self._uni.get(name, -1)

                    def set1f(name: str, value: float) -> None:
                        loc = uniform_location(name)
                        if loc != -1:
                            gf.glUniform1f(loc, float(value))

                    def set2f(name: str, x: float, y: float) -> None:
                        loc = uniform_location(name)
                        if loc != -1:
                            gf.glUniform2f(loc, float(x), float(y))

                    def set3f(name: str, x: float, y: float, z: float) -> None:
                        loc = uniform_location(name)
                        if loc != -1:
                            gf.glUniform3f(loc, float(x), float(y), float(z))

                    gf.glActiveTexture(gl.GL_TEXTURE0)
                    gf.glBindTexture(gl.GL_TEXTURE_2D, int(self._tex_id))
                    tex_loc = uniform_location("uTex")
                    if tex_loc != -1:
                        gf.glUniform1i(tex_loc, 0)

                    def adjustment_value(key: str, default: float = 0.0) -> float:
                        return float(adj.get(key, default))

                    set1f("uBrilliance", adjustment_value("Brilliance"))
                    set1f("uExposure", adjustment_value("Exposure"))
                    set1f("uHighlights", adjustment_value("Highlights"))
                    set1f("uShadows", adjustment_value("Shadows"))
                    set1f("uBrightness", adjustment_value("Brightness"))
                    set1f("uContrast", adjustment_value("Contrast"))
                    set1f("uBlackPoint", adjustment_value("BlackPoint"))
                    set1f("uSaturation", adjustment_value("Saturation"))
                    set1f("uVibrance", adjustment_value("Vibrance"))
                    set1f("uColorCast", adjustment_value("Cast"))

                    set3f(
                        "uGain",
                        float(adj.get("Color_Gain_R", 1.0)),
                        float(adj.get("Color_Gain_G", 1.0)),
                        float(adj.get("Color_Gain_B", 1.0)),
                    )

                    view_width = float(width)
                    view_height = float(height)
                    base_scale = self._fit_to_view_scale(view_width, view_height)
                    effective_scale = max(base_scale, 1e-6)
                    set1f("uScale", effective_scale)
                    set2f("uViewSize", view_width, view_height)
                    set2f("uTexSize", float(max(1, self._tex_w)), float(max(1, self._tex_h)))
                    set2f("uPan", 0.0, 0.0)

                    gf.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

                    if self._dummy_vao:
                        self._dummy_vao.release()
                finally:
                    prog.release()

                # ``toImage`` performs a synchronous read-back which is acceptable for the
                # modest preview sizes involved here and dramatically simpler than wiring up
                # asynchronous PBO downloads.
                return fbo.toImage().convertToFormat(QImage.Format.Format_ARGB32)
            finally:
                fbo.release()
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous_fbo)
                try:
                    x, y, w, h = [int(v) for v in previous_viewport]
                    gf.glViewport(x, y, w, h)
                except Exception:
                    # ``glGetIntegerv`` may return ctypes arrays that do not iterate cleanly on
                    # some drivers.  Silently skip viewport restoration if conversion fails; the
                    # widget will correct the viewport the next time ``paintGL`` runs.
                    pass
        finally:
            self.doneCurrent()

        return QImage()

    # --------------------------- GL lifecycle ---------------------------

    def initializeGL(self) -> None:
        from OpenGL import GL as gl

        # GL funcs
        self._gl_funcs = QOpenGLFunctions_3_3_Core()
        self._gl_funcs.initializeOpenGLFunctions()
        self._gl_extra = self.context().extraFunctions()
        self._gl_extra.initializeOpenGLFunctions()
        gf = self._gl_funcs

        # Debug logger
        try:
            self._logger = QOpenGLDebugLogger(self)
            if self._logger.initialize():
                self._logger.messageLogged.connect(
                    lambda m: print(f"[GLDBG] {m.source().name}: {m.message()}")
                )
                self._logger.startLogging(QOpenGLDebugLogger.SynchronousLogging)
                print("[GLDBG] DebugLogger initialized.")
            else:
                print("[GLDBG] DebugLogger not available.")
        except Exception as e:
            print(f"[GLDBG] Logger init failed: {e}")

        # Dummy VAO
        self._dummy_vao = QOpenGLVertexArrayObject(self)
        self._dummy_vao.create()
        print(f"[GL INIT] Dummy VAO created: {self._dummy_vao.isCreated()}")

        # Shaders
        vert_src = r"""
            #version 330 core
            out vec2 vUV;
            void main() {
                const vec2 POS[3] = vec2[3](
                    vec2(-1.0, -1.0),
                    vec2( 3.0, -1.0),
                    vec2(-1.0,  3.0)
                );
                const vec2 UVS[3] = vec2[3](
                    vec2(0.0, 0.0),
                    vec2(2.0, 0.0),
                    vec2(0.0, 2.0)
                );
                vUV = UVS[gl_VertexID];
                gl_Position = vec4(POS[gl_VertexID], 0.0, 1.0);
            }
        """

        frag_src = r"""
            #version 330 core
            in vec2 vUV;
            out vec4 FragColor;

            uniform sampler2D uTex;

            uniform float uBrilliance;
            uniform float uExposure;
            uniform float uHighlights;
            uniform float uShadows;
            uniform float uBrightness;
            uniform float uContrast;
            uniform float uBlackPoint;
            uniform float uSaturation;
            uniform float uVibrance;
            uniform float uColorCast;   // <- 原 uCast 改名
            uniform vec3  uGain;
            uniform vec2  uViewSize;
            uniform vec2  uTexSize;
            uniform float uScale;
            uniform vec2  uPan;

            float clamp01(float x) { return clamp(x, 0.0, 1.0); }

            float apply_channel(float value,
                                float exposure,
                                float brightness,
                                float brilliance,
                                float highlights,
                                float shadows,
                                float contrast_factor,
                                float black_point)
            {
                float adjusted = value + exposure + brightness;
                float mid_distance = value - 0.5;
                adjusted += brilliance * (1.0 - pow(mid_distance * 2.0, 2.0));

                if (adjusted > 0.65) {
                    float ratio = (adjusted - 0.65) / 0.35;
                    adjusted += highlights * ratio;
                } else if (adjusted < 0.35) {
                    float ratio = (0.35 - adjusted) / 0.35;
                    adjusted += shadows * ratio;
                }

                adjusted = (adjusted - 0.5) * contrast_factor + 0.5;

                if (black_point > 0.0)
                    adjusted -= black_point * (1.0 - adjusted);
                else if (black_point < 0.0)
                    adjusted -= black_point * adjusted;

                return clamp01(adjusted);
            }

            vec3 apply_color_transform(vec3 rgb,
                                       float saturation,
                                       float vibrance,
                                       float colorCast,  // <- 参数改名
                                       vec3 gain)
            {
                vec3 mixGain = (1.0 - colorCast) + gain * colorCast;
                rgb *= mixGain;

                float luma = dot(rgb, vec3(0.299, 0.587, 0.114));
                vec3  chroma = rgb - vec3(luma);
                float sat_amt = 1.0 + saturation;
                float vib_amt = 1.0 + vibrance;
                float w = 1.0 - clamp(abs(luma - 0.5) * 2.0, 0.0, 1.0);
                float chroma_scale = sat_amt * (1.0 + (vib_amt - 1.0) * w);
                chroma *= chroma_scale;
                return clamp(vec3(luma) + chroma, 0.0, 1.0);
            }

            void main() {
                if (uScale <= 0.0) {
                    discard;
                }

                // Convert the fragment's window coordinates into a centred viewport
                // frame before undoing the zoom/pan transform. ``gl_FragCoord`` is
                // defined with its origin at the bottom-left corner and references
                // pixel centres, so subtracting 0.5 yields a zero-based pixel grid.
                vec2 fragPx = vec2(gl_FragCoord.x - 0.5, gl_FragCoord.y - 0.5);
                vec2 viewCentre = uViewSize * 0.5;
                vec2 viewVector = fragPx - viewCentre;
                vec2 texVector = (viewVector - uPan) / uScale;
                vec2 texPx = texVector + (uTexSize * 0.5);
                vec2 uv = texPx / uTexSize;

                if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
                    discard;
                }

                // The uploaded QImage data uses a top-left origin, so flip the
                // vertical axis when sampling.
                uv.y = 1.0 - uv.y;

                vec4 texel = texture(uTex, uv);
                vec3 c = texel.rgb;

                float exposure_term    = uExposure   * 1.5;
                float brightness_term  = uBrightness * 0.75;
                float brilliance_term  = uBrilliance * 0.6;
                float contrast_factor  = 1.0 + uContrast;

                c.r = apply_channel(c.r, exposure_term, brightness_term, brilliance_term,
                                    uHighlights, uShadows, contrast_factor, uBlackPoint);
                c.g = apply_channel(c.g, exposure_term, brightness_term, brilliance_term,
                                    uHighlights, uShadows, contrast_factor, uBlackPoint);
                c.b = apply_channel(c.b, exposure_term, brightness_term, brilliance_term,
                                    uHighlights, uShadows, contrast_factor, uBlackPoint);

                c = apply_color_transform(c, uSaturation, uVibrance, uColorCast, uGain);
                FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
            }
        """

        self._shader_program = QOpenGLShaderProgram(self)
        prog = self._shader_program
        ok_v = prog.addShaderFromSourceCode(QOpenGLShader.Vertex, vert_src)
        if not ok_v:
            print("[GL ERROR] Vertex shader compile failed:", prog.log())
            return
        ok_f = prog.addShaderFromSourceCode(QOpenGLShader.Fragment, frag_src)
        if not ok_f:
            print("[GL ERROR] Fragment shader compile failed:", prog.log())
            return
        if not prog.link():
            print("[GL ERROR] Program link failed:", prog.log())
            return
        print("[GL INIT] Shader linked OK")

        names = [
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
            "uViewSize",
            "uTexSize",
            "uScale",
            "uPan",
        ]
        self._uni = {n: prog.uniformLocation(n) for n in names}
        print("[GL INIT] Uniforms:", self._uni)

        self._tex_id = 0
        gf.glDisable(gl.GL_DEPTH_TEST)
        gf.glDisable(gl.GL_CULL_FACE)
        gf.glDisable(gl.GL_BLEND)

        dpr = self.devicePixelRatioF()
        gf.glViewport(0, 0, int(self.width() * dpr), int(self.height() * dpr))
        print("[GL INIT] initializeGL completed.")

    def paintGL(self) -> None:
        gf = self._gl_funcs
        if gf is None or self._shader_program is None:
            return
        from OpenGL import GL as gl

        # 1) Configure the viewport to match the widget and paint the shared
        # backdrop colour so zoomed or letterboxed regions blend into the chrome.
        dpr = self.devicePixelRatioF()
        vw = max(1, int(round(self.width() * dpr)))
        vh = max(1, int(round(self.height() * dpr)))
        gf.glViewport(0, 0, vw, vh)
        bg = self._backdrop_color
        gf.glClearColor(bg.redF(), bg.greenF(), bg.blueF(), 1.0)
        gf.glClear(gl.GL_COLOR_BUFFER_BIT)

        # 2) 纹理延迟上传
        if getattr(self, "_tex_id", 0) == 0 and getattr(self, "_image", None) and not self._image.isNull():
            self._upload_texture_raw_gl(self._image)
        if self._tex_id == 0:
            return

        prog = self._shader_program
        if not prog.bind():
            print("[GL DBG] program bind failed:", prog.log())
            return

        # 3) 绑定 VAO
        if self._dummy_vao:
            self._dummy_vao.bind()

        # 4) 绑定纹理与采样器（显式 1i）
        gf.glActiveTexture(gl.GL_TEXTURE0)
        gf.glBindTexture(gl.GL_TEXTURE_2D, int(self._tex_id))
        loc = self._uni.get("uTex", -1)
        if loc != -1:
            gf.glUniform1i(loc, 0)

        # 5) 调色参数（显式 1f/3f）
        adj = getattr(self, "_adjustments", {}) or {}

        def f(k, d=0.0):
            return float(adj.get(k, d))

        def set1f(name, val):
            l = self._uni.get(name, -1)
            if l != -1:
                gf.glUniform1f(l, float(val))

        set1f("uBrilliance", f("Brilliance"))
        set1f("uExposure", f("Exposure"))
        set1f("uHighlights", f("Highlights"))
        set1f("uShadows", f("Shadows"))
        set1f("uBrightness", f("Brightness"))
        set1f("uContrast", f("Contrast"))
        set1f("uBlackPoint", f("BlackPoint"))
        set1f("uSaturation", f("Saturation"))
        set1f("uVibrance", f("Vibrance"))
        set1f("uColorCast", f("Cast"))

        loc_gain = self._uni.get("uGain", -1)
        if loc_gain != -1:
            gf.glUniform3f(
                loc_gain,
                float(adj.get("Color_Gain_R", 1.0)),
                float(adj.get("Color_Gain_G", 1.0)),
                float(adj.get("Color_Gain_B", 1.0)),
            )

        # Provide the transform uniforms that reproduce the legacy pixmap viewer's
        # "fit to window" baseline while allowing additional zoom and pan offsets.
        view_width = float(vw)
        view_height = float(vh)
        base_scale = self._fit_to_view_scale(view_width, view_height)
        effective_scale = max(base_scale * self._zoom_factor, 1e-6)

        set1f("uScale", effective_scale)

        def set2f(name: str, x: float, y: float) -> None:
            loc = self._uni.get(name, -1)
            if loc != -1:
                gf.glUniform2f(loc, float(x), float(y))

        set2f("uViewSize", view_width, view_height)
        set2f("uTexSize", max(1.0, float(self._tex_w)), max(1.0, float(self._tex_h)))
        set2f("uPan", float(self._pan_px.x()), float(self._pan_px.y()))

        # 6) 绘制
        gf.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

        if self._dummy_vao:
            self._dummy_vao.release()
        prog.release()

        err = gf.glGetError()
        if err != gl.GL_NO_ERROR:
            print(f"[GL ERROR] After draw: 0x{int(err):04X}")

    # --------------------------- Texture (raw GL) ---------------------------

    def _delete_raw_texture(self):
        if self._tex_id:
            gl.glDeleteTextures(1, np.array([int(self._tex_id)], dtype=np.uint32))
            self._tex_id = 0
            self._tex_w = self._tex_h = 0

    def _upload_texture_raw_gl(self, img: QImage) -> None:
        from OpenGL import GL as gl
        import ctypes
        from PySide6.QtGui import QImage

        if img is None or img.isNull():
            if getattr(self, "_tex_id", 0):
                gl.glDeleteTextures([int(self._tex_id)])
                self._tex_id = 0
            return

        img = img.convertToFormat(QImage.Format_RGBA8888)
        w, h = img.width(), img.height()
        buf = img.constBits()
        nbytes = img.sizeInBytes()
        if hasattr(buf, "setsize"):
            buf.setsize(nbytes)
        else:
            buf = buf[:nbytes]

        if getattr(self, "_tex_id", 0):
            gl.glDeleteTextures([int(self._tex_id)])
            self._tex_id = 0

        tex = gl.glGenTextures(1)
        if isinstance(tex, (list, tuple)): tex = tex[0]
        self._tex_id = int(tex)
        # Track the texture size for aspect-ratio aware viewport calculations.
        self._tex_w = int(w)
        self._tex_h = int(h)

        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex_id)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        row_len = img.bytesPerLine() // 4
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, row_len)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, w, h, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, buf)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        err = gl.glGetError()
        print(f"[RAWGL] tex={self._tex_id} level0={w}x{h} glError=0x{int(err):X}")

    # --------------------------- Events ---------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            if self._live_replay_enabled:
                self.replayRequested.emit()
            else:
                self._is_panning = True
                self._pan_start_pos = event.position()
                self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_panning:
            # 以视口像素为单位叠加 pan
            d = event.position() - self._pan_start_pos
            self._pan_start_pos = event.position()
            dpr = self.devicePixelRatioF()
            self._pan_px += QPointF(d.x() * dpr, -d.y() * dpr)  # Y 轴向上为正
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = False
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            top_level = self.window()
            # Toggle immersive mode depending on the top-level window state.
            if top_level is not None and top_level.isFullScreen():
                self.fullscreenExitRequested.emit()
            else:
                self.fullscreenToggleRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._wheel_action == "zoom":
            angle = event.angleDelta().y()
            if angle > 0:
                self.set_zoom(self._zoom_factor * 1.1, anchor=event.position())
            elif angle < 0:
                self.set_zoom(self._zoom_factor / 1.1, anchor=event.position())
        else:
            delta = event.angleDelta()
            step = delta.y() or delta.x()
            if step < 0:
                self.nextItemRequested.emit()
            elif step > 0:
                self.prevItemRequested.emit()
        event.accept()

    def resizeGL(self, w: int, h: int) -> None:
        gf = self._gl_funcs
        if not gf:
            return
        dpr = self.devicePixelRatioF()
        gf.glViewport(0, 0, max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._loading_overlay is not None:
            self._loading_overlay.resize(self.size())

    def _fit_to_view_scale(self, view_width: float, view_height: float) -> float:
        """Return the baseline scale that fits the texture within the viewport."""

        if self._tex_w <= 0 or self._tex_h <= 0:
            return 1.0
        if view_width <= 0.0 or view_height <= 0.0:
            return 1.0
        width_ratio = view_width / float(self._tex_w)
        height_ratio = view_height / float(self._tex_h)
        scale = min(width_ratio, height_ratio)
        if scale <= 0.0:
            return 1.0
        return scale

    @staticmethod
    def _normalise_colour(value: QColor | str) -> QColor:
        """Return a valid ``QColor`` derived from *value* (defaulting to black)."""

        colour = QColor(value)
        if not colour.isValid():
            colour = QColor("#000000")
        return colour

    def _apply_surface_color(self) -> None:
        """Synchronise the widget stylesheet and GL clear colour backdrop."""

        target = self._surface_override or self._default_surface_color
        self.setStyleSheet(f"background-color: {target.name()}; border: none;")
        self._backdrop_color = QColor(target)
        self.update()
