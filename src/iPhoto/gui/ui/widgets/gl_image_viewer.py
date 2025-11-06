# -*- coding: utf-8 -*-
"""
GPU-accelerated image viewer (pure OpenGL texture upload; pixel-accurate zoom/pan).
- Ensures magnification samples the ORIGINAL pixels (no Qt/FBO resampling).
- Uses GL 3.3 Core, VAO/VBO, and a raw glTexImage2D + glTexSubImage2D upload path.
"""

from __future__ import annotations

import ctypes
import numpy as np
from typing import Mapping, Optional, Tuple

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QWheelEvent,
    QVector2D,
    QSurfaceFormat, QOpenGLContext,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFunctions_3_3_Core,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLVertexArrayObject, QOpenGLDebugLogger,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from OpenGL import GL as gl

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
        self._zoom_factor: float = 1.0
        self._min_zoom: float = 0.1
        self._max_zoom: float = 16.0
        self._pan_px: QPointF = QPointF(0.0, 0.0)  # 以“视口像素”为单位的平移
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
    ) -> None:
        """Display *image* together with optional colour *adjustments*."""

        self._image = image
        mapped_adjustments = dict(adjustments or {})
        self._pending_adjustments = mapped_adjustments
        self._adjustments = mapped_adjustments

        if image is None or image.isNull():
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

        # 只请求重绘；真正的上传放到 paintGL（上下文 current）
        self.update()
    def set_placeholder(self, pixmap) -> None:
        """Placeholder -> 也按 set_image 路径走。"""
        if pixmap and not pixmap.isNull():
            self.set_image(pixmap.toImage(), {})
        else:
            self.set_image(None, {})

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
        clamped = max(self._min_zoom, min(self._max_zoom, float(factor)))
        if abs(clamped - self._zoom_factor) < 1e-6:
            return
        # 以锚点为中心缩放：调整 pan_px 使锚点保持稳定
        if anchor is not None:
            dpr = self.devicePixelRatioF()
            view_anchor_px = QPointF(anchor.x() * dpr, (self.height() - anchor.y()) * dpr)
            old_zoom = self._zoom_factor
            new_zoom = clamped
            # 目标： (fragPx - pan)/zoom 保持不变 => Δpan = fragPx*(1 - new/old)
            if old_zoom > 0:
                delta = view_anchor_px * (1.0 - new_zoom / old_zoom)
                self._pan_px += delta
        self._zoom_factor = clamped
        self.update()
        self.zoomChanged.emit(self._zoom_factor)

    def reset_zoom(self) -> None:
        self._zoom_factor = 1.0
        self._pan_px = QPointF(0.0, 0.0)
        self.update()
        self.zoomChanged.emit(self._zoom_factor)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom_factor * 1.1)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom_factor / 1.1)

    def viewport_center(self) -> QPointF:
        return QPointF(self.width() / 2, self.height() / 2)

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
                vec2 uv = clamp(vUV, 0.0, 1.0);
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
            "uTex", "uBrilliance", "uExposure", "uHighlights", "uShadows", "uBrightness",
            "uContrast", "uBlackPoint", "uSaturation", "uVibrance", "uColorCast", "uGain"
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

        # 1) 视口 & 清屏
        dpr = self.devicePixelRatioF()
        vw, vh = int(self.width() * dpr), int(self.height() * dpr)
        gf.glViewport(0, 0, vw, vh)
        bg = self._backdrop_color
        gf.glClearColor(bg.redF(), bg.greenF(), bg.blueF(), 1.0)
        gf.glClear(gl.GL_COLOR_BUFFER_BIT)

        # 2) 纹理延迟上传
        if getattr(self, "_tex_id", 0) == 0 and getattr(self, "_image", None) and not self._image.isNull():
            self._upload_texture_raw_gl(self._image)
        if self._tex_id == 0:
            return

        # Ensure the rendered quad preserves the source aspect ratio by letterboxing.
        viewport = self._calculate_letterboxed_viewport(vw, vh)
        gf.glViewport(*viewport)

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
        target = self._calculate_letterboxed_viewport(int(w * dpr), int(h * dpr))
        gf.glViewport(*target)

    def _calculate_letterboxed_viewport(self, view_width: int, view_height: int) -> Tuple[int, int, int, int]:
        """Compute a centred viewport that preserves the texture aspect ratio."""

        if view_width <= 0 or view_height <= 0:
            return 0, 0, max(view_width, 0), max(view_height, 0)
        if self._tex_w <= 0 or self._tex_h <= 0:
            return 0, 0, view_width, view_height

        texture_aspect = self._tex_w / self._tex_h
        view_aspect = view_width / view_height

        if abs(texture_aspect - view_aspect) < 1e-6:
            return 0, 0, view_width, view_height

        if view_aspect > texture_aspect:
            target_height = view_height
            target_width = int(round(target_height * texture_aspect))
        else:
            target_width = view_width
            target_height = int(round(target_width / texture_aspect))

        target_width = max(1, min(target_width, view_width))
        target_height = max(1, min(target_height, view_height))

        offset_x = (view_width - target_width) // 2
        offset_y = (view_height - target_height) // 2
        return offset_x, offset_y, target_width, target_height

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
