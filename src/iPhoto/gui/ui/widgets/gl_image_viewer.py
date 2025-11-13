# -*- coding: utf-8 -*-
"""
GPU-accelerated image viewer (pure OpenGL texture upload; pixel-accurate zoom/pan).
- Ensures magnification samples the ORIGINAL pixels (no Qt/FBO resampling).
- Uses GL 3.3 Core, VAO/VBO, and a raw glTexImage2D + glTexSubImage2D upload path.
"""

from __future__ import annotations

from typing import Mapping, Optional

import enum
import logging
import math
import time

from PySide6.QtCore import QPointF, QSize, Qt, Signal, QTimer
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QWheelEvent,
    QSurfaceFormat,
    QPixmap,
)
from PySide6.QtOpenGL import (
    QOpenGLFramebufferObject,
    QOpenGLFramebufferObjectFormat,
    QOpenGLFunctions_3_3_Core,
    QOpenGLDebugLogger,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel
from OpenGL import GL as gl

from .gl_renderer import GLRenderer
from .view_transform_controller import (
    ViewTransformController,
    compute_fit_to_view_scale,
)

_LOGGER = logging.getLogger(__name__)

# 如果你的工程没有这个函数，可以改成固定背景色
try:
    from ..palette import viewer_surface_color  # type: ignore
except Exception:
    def viewer_surface_color(_):  # fallback
        return QColor(0, 0, 0)


class CropHandle(enum.IntEnum):
    NONE = 0
    LEFT = 1
    RIGHT = 2
    BOTTOM = 3
    TOP = 4
    TOP_LEFT = 5
    TOP_RIGHT = 6
    BOTTOM_RIGHT = 7
    BOTTOM_LEFT = 8
    INSIDE = -1


def cursor_for_handle(handle: CropHandle) -> Qt.CursorShape:
    return {
        CropHandle.LEFT: Qt.CursorShape.SizeHorCursor,
        CropHandle.RIGHT: Qt.CursorShape.SizeHorCursor,
        CropHandle.TOP: Qt.CursorShape.SizeVerCursor,
        CropHandle.BOTTOM: Qt.CursorShape.SizeVerCursor,
        CropHandle.TOP_LEFT: Qt.CursorShape.SizeFDiagCursor,
        CropHandle.BOTTOM_RIGHT: Qt.CursorShape.SizeFDiagCursor,
        CropHandle.TOP_RIGHT: Qt.CursorShape.SizeBDiagCursor,
        CropHandle.BOTTOM_LEFT: Qt.CursorShape.SizeBDiagCursor,
        CropHandle.INSIDE: Qt.CursorShape.OpenHandCursor,
    }.get(handle, Qt.CursorShape.ArrowCursor)


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_in_quad(t: float) -> float:
    return t * t


class CropBoxState:
    """Normalised crop rectangle maintained while crop mode is active."""

    def __init__(self) -> None:
        self.cx: float = 0.5
        self.cy: float = 0.5
        self.width: float = 1.0
        self.height: float = 1.0
        self.min_width: float = 0.02
        self.min_height: float = 0.02

    def set_from_mapping(self, values: Mapping[str, float]) -> None:
        self.cx = float(values.get("Crop_CX", 0.5))
        self.cy = float(values.get("Crop_CY", 0.5))
        self.width = float(values.get("Crop_W", 1.0))
        self.height = float(values.get("Crop_H", 1.0))
        self.clamp()

    def as_mapping(self) -> dict[str, float]:
        return {
            "Crop_CX": float(self.cx),
            "Crop_CY": float(self.cy),
            "Crop_W": float(self.width),
            "Crop_H": float(self.height),
        }

    def set_full(self) -> None:
        self.cx = 0.5
        self.cy = 0.5
        self.width = 1.0
        self.height = 1.0

    def bounds_normalised(self) -> tuple[float, float, float, float]:
        half_w = self.width * 0.5
        half_h = self.height * 0.5
        return (
            self.cx - half_w,
            self.cy - half_h,
            self.cx + half_w,
            self.cy + half_h,
        )

    def to_pixel_rect(self, image_width: int, image_height: int) -> dict[str, float]:
        left_n, top_n, right_n, bottom_n = self.bounds_normalised()
        return {
            "left": left_n * image_width,
            "top": top_n * image_height,
            "right": right_n * image_width,
            "bottom": bottom_n * image_height,
        }

    def center_pixels(self, image_width: int, image_height: int) -> QPointF:
        return QPointF(self.cx * image_width, self.cy * image_height)

    def translate_pixels(self, delta: QPointF, image_size: tuple[int, int]) -> None:
        iw, ih = image_size
        if iw <= 0 or ih <= 0:
            return
        self.cx += float(delta.x()) / float(iw)
        self.cy += float(delta.y()) / float(ih)
        self.clamp()

    def drag_edge_pixels(self, handle: CropHandle, delta: QPointF, image_size: tuple[int, int]) -> None:
        iw, ih = image_size
        if iw <= 0 or ih <= 0:
            return
        dx = float(delta.x()) / float(iw)
        dy = float(delta.y()) / float(ih)
        left, top, right, bottom = self.bounds_normalised()
        min_w = max(self.min_width, 1.0 / max(1.0, float(iw)))
        min_h = max(self.min_height, 1.0 / max(1.0, float(ih)))

        if handle in (CropHandle.LEFT, CropHandle.TOP_LEFT, CropHandle.BOTTOM_LEFT):
            left = min(max(0.0, left + dx), right - min_w)
        if handle in (CropHandle.RIGHT, CropHandle.TOP_RIGHT, CropHandle.BOTTOM_RIGHT):
            right = max(min(1.0, right + dx), left + min_w)
        if handle in (CropHandle.TOP, CropHandle.TOP_LEFT, CropHandle.TOP_RIGHT):
            top = min(max(0.0, top + dy), bottom - min_h)
        if handle in (CropHandle.BOTTOM, CropHandle.BOTTOM_LEFT, CropHandle.BOTTOM_RIGHT):
            bottom = max(min(1.0, bottom + dy), top + min_h)

        width = max(min_w, min(1.0, right - left))
        height = max(min_h, min(1.0, bottom - top))
        self.width = width
        self.height = height
        self.cx = left + width * 0.5
        self.cy = top + height * 0.5
        self.clamp()

    def clamp(self) -> None:
        self.width = max(self.min_width, min(1.0, self.width))
        self.height = max(self.min_height, min(1.0, self.height))
        half_w = self.width * 0.5
        half_h = self.height * 0.5
        self.cx = max(half_w, min(1.0 - half_w, self.cx))
        self.cy = max(half_h, min(1.0 - half_h, self.cy))

class GLImageViewer(QOpenGLWidget):
    """A QWidget that displays GPU-rendered images with pixel-accurate zoom."""

    # Signals（保持与旧版一致）
    replayRequested = Signal()
    zoomChanged = Signal(float)
    nextItemRequested = Signal()
    prevItemRequested = Signal()
    fullscreenExitRequested = Signal()
    fullscreenToggleRequested = Signal()
    cropChanged = Signal(float, float, float, float)

    def __init__(self, parent: Optional["QOpenGLWidget"] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)

        # 强制 3.3 Core
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)
        self._gl_funcs: Optional[QOpenGLFunctions_3_3_Core] = None
        self._renderer: Optional[GLRenderer] = None
        self._logger: Optional[QOpenGLDebugLogger] = None

        # 状态
        self._image: Optional[QImage] = None
        self._adjustments: dict[str, float] = {}
        self._current_image_source: Optional[object] = None
        self._live_replay_enabled: bool = False

        # Track the viewer surface colour so immersive mode can temporarily
        # switch to a pure black canvas.  ``viewer_surface_color`` returns a
        # palette-derived colour string, which we normalise to ``QColor`` for
        # reliable comparisons and GL clear colour conversion.
        self._default_surface_color = self._normalise_colour(viewer_surface_color(self))
        self._surface_override: Optional[QColor] = None
        self._backdrop_color: QColor = QColor(self._default_surface_color)
        self._apply_surface_color()

        # ``_time_base`` anchors the monotonic clock used by the shader grain generator.  Resetting
        # the start time keeps the uniform values numerically small even after long application
        # sessions.
        self._time_base = time.monotonic()

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
        self._transform_controller = ViewTransformController(
            self,
            texture_size_provider=self._texture_dimensions,
            on_zoom_changed=self.zoomChanged.emit,
            on_next_item=self.nextItemRequested.emit,
            on_prev_item=self.prevItemRequested.emit,
        )
        self._transform_controller.reset_zoom()

        # Crop interaction state -------------------------------------------------
        self._crop_mode: bool = False
        self._crop_state = CropBoxState()
        self._crop_drag_handle: CropHandle = CropHandle.NONE
        self._crop_dragging: bool = False
        self._crop_last_pos = QPointF()
        self._crop_hit_padding: float = 12.0
        self._crop_edge_threshold: float = 48.0
        self._crop_idle_timer = QTimer(self)
        self._crop_idle_timer.setInterval(1000)
        self._crop_idle_timer.timeout.connect(self._on_crop_idle_timeout)
        self._crop_anim_timer = QTimer(self)
        self._crop_anim_timer.setInterval(16)
        self._crop_anim_timer.timeout.connect(self._on_crop_anim_tick)
        self._crop_anim_active: bool = False
        self._crop_anim_start_time: float = 0.0
        self._crop_anim_duration: float = 0.3
        self._crop_anim_start_scale: float = 1.0
        self._crop_anim_target_scale: float = 1.0
        self._crop_anim_start_center = QPointF()
        self._crop_anim_target_center = QPointF()
        self._crop_faded_out: bool = False

    # --------------------------- Public API ---------------------------

    def shutdown(self) -> None:
        """Clean up GL resources."""
        self.makeCurrent()
        try:
            if self._renderer is not None:
                self._renderer.destroy_resources()
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
        self._adjustments = dict(adjustments or {})
        self._loading_overlay.hide()
        self._time_base = time.monotonic()

        if image is None or image.isNull():
            self._current_image_source = None
            renderer = self._renderer
            if renderer is not None:
                gl_context = self.context()
                if gl_context is not None:
                    # ``set_image(None)`` is frequently triggered while the widget is
                    # still hidden, meaning the GL context (and therefore the
                    # renderer) may not have been created yet.  Guard the cleanup so
                    # we only touch GPU state when a live context is bound.
                    self.makeCurrent()
                    try:
                        renderer.delete_texture()
                    finally:
                        self.doneCurrent()

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
        self._transform_controller.set_wheel_action(action)

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

        anchor_point = anchor or self.viewport_center()
        self._transform_controller.set_zoom(float(factor), anchor_point)

    def reset_zoom(self) -> None:
        self._transform_controller.reset_zoom()

    def zoom_in(self) -> None:
        current = self._transform_controller.get_zoom_factor()
        self.set_zoom(current * 1.1, anchor=self.viewport_center())

    def zoom_out(self) -> None:
        current = self._transform_controller.get_zoom_factor()
        self.set_zoom(current / 1.1, anchor=self.viewport_center())

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

        if self.context() is None:
            _LOGGER.warning("render_offscreen_image: no OpenGL context available")
            return QImage()

        if self._image is None or self._image.isNull():
            _LOGGER.warning("render_offscreen_image: no source image bound to the viewer")
            return QImage()

        self.makeCurrent()
        try:
            if self._gl_funcs is None or self._renderer is None:
                # Off-screen rendering can be triggered before the widget ever hits the
                # on-screen GL lifecycle (e.g. a preview request while the window is
                # still hidden).  Creating the renderer here would immediately be undone
                # by ``initializeGL`` because Qt rebuilds the context once the widget is
                # shown.  Instead of doing redundant work, bail out and let the caller
                # retry after the viewer is fully initialised.
                _LOGGER.warning(
                    "render_offscreen_image: renderer not initialized, skipping."
                )
                return QImage()

            gf = self._gl_funcs
            assert gf is not None, "_gl_funcs should be set when renderer exists"

            if not self._renderer.has_texture():
                self._renderer.upload_texture(self._image)
            if not self._renderer.has_texture():
                _LOGGER.error("render_offscreen_image: texture upload failed")
                return QImage()

            width = max(1, int(target_size.width()))
            height = max(1, int(target_size.height()))

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

                texture_size = self._renderer.texture_size()
                base_scale = compute_fit_to_view_scale(texture_size, float(width), float(height))
                effective_scale = max(base_scale, 1e-6)
                time_value = time.monotonic() - self._time_base
                self._renderer.render(
                    view_width=float(width),
                    view_height=float(height),
                    scale=effective_scale,
                    pan=QPointF(0.0, 0.0),
                    adjustments=dict(adjustments or self._adjustments),
                    time_value=time_value,
                )

                return fbo.toImage().convertToFormat(QImage.Format.Format_ARGB32)
            finally:
                fbo.release()
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous_fbo)
                try:
                    x, y, w, h = [int(v) for v in previous_viewport]
                    gf.glViewport(x, y, w, h)
                except Exception:
                    pass
        finally:
            self.doneCurrent()

        return QImage()

    # --------------------------- GL lifecycle ---------------------------

    def initializeGL(self) -> None:
        self._gl_funcs = QOpenGLFunctions_3_3_Core()
        self._gl_funcs.initializeOpenGLFunctions()
        gf = self._gl_funcs

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
        except Exception as exc:
            print(f"[GLDBG] Logger init failed: {exc}")

        if self._renderer is not None:
            self._renderer.destroy_resources()

        self._renderer = GLRenderer(gf, parent=self)
        self._renderer.initialize_resources()

        dpr = self.devicePixelRatioF()
        gf.glViewport(0, 0, int(self.width() * dpr), int(self.height() * dpr))
        print("[GL INIT] initializeGL completed.")

    def paintGL(self) -> None:
        gf = self._gl_funcs
        if gf is None or self._renderer is None:
            return

        dpr = self.devicePixelRatioF()
        vw = max(1, int(round(self.width() * dpr)))
        vh = max(1, int(round(self.height() * dpr)))
        gf.glViewport(0, 0, vw, vh)
        bg = self._backdrop_color
        gf.glClearColor(bg.redF(), bg.greenF(), bg.blueF(), 1.0)
        gf.glClear(gl.GL_COLOR_BUFFER_BIT)

        if self._image is not None and not self._image.isNull() and not self._renderer.has_texture():
            self._renderer.upload_texture(self._image)
        if not self._renderer.has_texture():
            return

        texture_size = self._renderer.texture_size()
        base_scale = compute_fit_to_view_scale(texture_size, float(vw), float(vh))
        zoom_factor = self._transform_controller.get_zoom_factor()
        effective_scale = max(base_scale * zoom_factor, 1e-6)

        time_value = time.monotonic() - self._time_base

        self._renderer.render(
            view_width=float(vw),
            view_height=float(vh),
            scale=effective_scale,
            pan=self._transform_controller.get_pan_pixels(),
            adjustments=self._adjustments,
            time_value=time_value,
        )

        if self._crop_mode:
            crop_rect = self._current_crop_rect_pixels()
            if crop_rect is not None:
                self._renderer.draw_crop_overlay(
                    view_width=float(vw),
                    view_height=float(vh),
                    crop_rect=crop_rect,
                    faded=self._crop_faded_out,
                )

    # --------------------------- Crop helpers ---------------------------

    def setCropMode(self, enabled: bool, values: Optional[Mapping[str, float]] = None) -> None:
        if enabled == self._crop_mode:
            if enabled and values is not None:
                self._apply_crop_values(values)
            return

        self._crop_mode = bool(enabled)
        if not self._crop_mode:
            self._stop_crop_animation()
            self._crop_idle_timer.stop()
            self._crop_drag_handle = CropHandle.NONE
            self._crop_dragging = False
            self._crop_faded_out = False
            self.unsetCursor()
            self.update()
            return

        self._apply_crop_values(values)
        self._crop_faded_out = False
        self._crop_drag_handle = CropHandle.NONE
        self._crop_dragging = False
        self._stop_crop_animation()
        self._restart_crop_idle()
        self.update()

    def crop_values(self) -> dict[str, float]:
        return self._crop_state.as_mapping()

    def _apply_crop_values(self, values: Optional[Mapping[str, float]]) -> None:
        if values:
            self._crop_state.set_from_mapping(values)
        else:
            self._crop_state.set_full()
        if not self._renderer or not self._renderer.has_texture():
            return
        center = self._crop_state.center_pixels(*self._renderer.texture_size())
        scale = self._effective_scale()
        clamped_center = self._clamp_image_center_to_crop(center, scale)
        self._set_image_center_pixels(clamped_center, scale=scale)

    def _view_dimensions_device_px(self) -> tuple[float, float]:
        dpr = self.devicePixelRatioF()
        vw = max(1.0, float(self.width()) * dpr)
        vh = max(1.0, float(self.height()) * dpr)
        return vw, vh

    def _screen_to_world(self, screen_pt: QPointF) -> QPointF:
        """Map a Qt screen coordinate to the GL view's centre-origin space.

        Qt reports positions in logical pixels with the origin at the top-left and
        a downward pointing Y axis.  The renderer however reasons about vectors in
        device pixels where the origin lives at the viewport centre and the Y axis
        grows upwards.  This helper performs the origin shift, the device pixel
        conversion and the Y flip so every caller receives a world-space vector
        that matches what the shader expects.
        """

        dpr = self.devicePixelRatioF()
        vw, vh = self._view_dimensions_device_px()
        sx = float(screen_pt.x()) * dpr
        sy = float(screen_pt.y()) * dpr
        world_x = sx - (vw * 0.5)
        world_y = (vh * 0.5) - sy
        return QPointF(world_x, world_y)

    def _world_to_screen(self, world_vec: QPointF) -> QPointF:
        """Convert a GL centre-origin vector into a Qt screen coordinate.

        The inverse of :meth:`_screen_to_world`: a world vector expressed in
        pixels relative to the viewport centre (Y up) is translated back into the
        top-left origin, Y-down coordinate system that Qt painting routines use.
        The return value is expressed in logical pixels to remain consistent with
        Qt's high-DPI handling.
        """

        dpr = self.devicePixelRatioF()
        vw, vh = self._view_dimensions_device_px()
        sx = float(world_vec.x()) + (vw * 0.5)
        sy = (vh * 0.5) - float(world_vec.y())
        return QPointF(sx / dpr, sy / dpr)

    def _effective_scale(self) -> float:
        if not self._renderer or not self._renderer.has_texture():
            return 1.0
        vw, vh = self._view_dimensions_device_px()
        base_scale = compute_fit_to_view_scale(self._renderer.texture_size(), vw, vh)
        zoom_factor = self._transform_controller.get_zoom_factor()
        return max(base_scale * zoom_factor, 1e-6)

    def _image_center_pixels(self) -> QPointF:
        if not self._renderer or not self._renderer.has_texture():
            return QPointF(0.0, 0.0)
        tex_w, tex_h = self._renderer.texture_size()
        scale = self._effective_scale()
        pan = self._transform_controller.get_pan_pixels()
        centre_x = (tex_w / 2.0) - (pan.x() / scale)
        # ``pan.y`` grows upwards in world space, therefore the corresponding
        # image coordinate moves towards the bottom (larger Y values) in the
        # conventional top-left origin texture space.
        centre_y = (tex_h / 2.0) + (pan.y() / scale)
        return QPointF(centre_x, centre_y)

    def _set_image_center_pixels(self, center: QPointF, *, scale: float | None = None) -> None:
        if not self._renderer or not self._renderer.has_texture():
            return
        tex_w, tex_h = self._renderer.texture_size()
        scale_value = scale if scale is not None else self._effective_scale()
        delta_x = center.x() - (tex_w / 2.0)
        delta_y = center.y() - (tex_h / 2.0)
        # ``delta_y`` measures how far the requested centre sits below the image
        # mid-line; in world space that translates to a positive upward pan.
        pan = QPointF(-delta_x * scale_value, delta_y * scale_value)
        self._transform_controller.set_pan_pixels(pan)

    def _clamp_image_center_to_crop(self, center: QPointF, scale: float) -> QPointF:
        """Clamp the image centre so the viewport continues covering the crop box.

        The crop rectangle is described in image pixel coordinates (origin in the
        top-left, *y* growing downward).  The view transform, on the other hand,
        pans within a GL style world space where the origin sits in the centre and
        *y* grows upwards.  By reasoning entirely in texture-space pixel units we
        avoid further conversions and can express the same constraints as the
        demo's ``_clamp_offset_to_cover_crop`` helper: ensure each edge of the
        zoomed viewport stays inside the crop rectangle before projecting the
        final centre back to world space.
        """

        if not self._renderer or not self._renderer.has_texture():
            return center

        tex_w, tex_h = self._renderer.texture_size()
        vw, vh = self._view_dimensions_device_px()
        half_view_w = (vw / scale) * 0.5
        half_view_h = (vh / scale) * 0.5
        crop_rect = self._crop_state.to_pixel_rect(tex_w, tex_h)

        # Compute the admissible centre range by demanding that every edge of the
        # viewport (centre ± half_view_*) remains outside-or-on the crop limits,
        # i.e. the zoomed viewport must fully cover the crop rectangle.  This
        # mirrors ``demo/crop_final.py`` where the camera offset is clamped so the
        # crop never peeks beyond the view.  Because the crop is expressed in a
        # top-left origin space we reason in the same coordinates to avoid any
        # extra axis flips.
        #
        # Horizontal bounds: the viewport's right edge (centre.x + half_view_w)
        # has to sit to the right of the crop's right edge.  Therefore the
        # minimum admissible centre is ``crop_rect["right"] - half_view_w``.
        def _clamp_axis(
            value: float,
            crop_min: float,
            crop_max: float,
            half_view: float,
            texture_extent: float,
        ) -> float:
            """Clamp a single axis so the viewport covers the crop rectangle.

            The helper mirrors ``demo/crop_final.py`` but folds in two extra edge
            cases that the production widget has to cope with:

            * When the zoomed viewport becomes narrower than the crop rectangle
              (``2 * half_view < crop_max - crop_min``) there exists no centre
              that satisfies the "cover" constraints.  In that scenario we fall
              back to the crop centre so that subsequent logic does not snap the
              image to one of the crop edges.
            * When the zoomed viewport is wider than the entire image (which can
              happen if the window is resized while highly zoomed out) the
              admissible range from the texture bounds collapses.  We collapse
              both ends to the image midpoint to avoid any oscillation.

            The final interval intersection is projected back onto the incoming
            value so the function behaves like a traditional clamp when the
            constraints are compatible.
            """

            # Bounds enforced by the image itself.  ``image_min``/``image_max``
            # describe where the viewport centre can live before exposing black
            # bars.
            image_min = half_view
            image_max = texture_extent - half_view
            if image_min > image_max:
                midpoint = texture_extent * 0.5
                image_min = image_max = midpoint

            # Bounds enforced by the crop coverage requirement.  The viewport
            # must extend at least as far left/right (or top/bottom) as the crop
            # rectangle.
            coverage_min = crop_max - half_view
            coverage_max = crop_min + half_view
            if coverage_min > coverage_max:
                midpoint = 0.5 * (crop_min + crop_max)
                coverage_min = coverage_max = midpoint

            lower = max(coverage_min, image_min)
            upper = min(coverage_max, image_max)
            if lower > upper:
                midpoint = 0.5 * (lower + upper)
                # Clamp once more against the image bounds so that the fallback
                # value never requests a pan beyond the texture limits.
                lower = upper = min(max(midpoint, min(image_min, image_max)), max(image_min, image_max))

            return min(max(value, lower), upper)

        clamped_x = _clamp_axis(
            center.x(),
            crop_rect["left"],
            crop_rect["right"],
            half_view_w,
            float(tex_w),
        )
        clamped_y = _clamp_axis(
            center.y(),
            crop_rect["top"],
            crop_rect["bottom"],
            half_view_h,
            float(tex_h),
        )
        return QPointF(clamped_x, clamped_y)

    def _crop_center_viewport_point(self) -> QPointF:
        if not self._renderer or not self._renderer.has_texture():
            return self.viewport_center()
        tex_w, tex_h = self._renderer.texture_size()
        center = self._crop_state.center_pixels(tex_w, tex_h)
        return self._image_to_viewport(center.x(), center.y())

    def _image_to_viewport(self, x: float, y: float) -> QPointF:
        if not self._renderer or not self._renderer.has_texture():
            return QPointF()
        scale = self._effective_scale()
        pan = self._transform_controller.get_pan_pixels()
        tex_w, tex_h = self._renderer.texture_size()
        tex_vector_x = x - (tex_w / 2.0)
        tex_vector_y = y - (tex_h / 2.0)
        world_vector = QPointF(
            tex_vector_x * scale + pan.x(),
            -(tex_vector_y * scale) + pan.y(),
        )
        # ``world_vector`` is now expressed in the GL-friendly centre-origin
        # space, so the last step is to convert it back to Qt's screen space for
        # hit testing and overlay rendering.
        return self._world_to_screen(world_vector)

    def _viewport_to_image(self, point: QPointF) -> QPointF:
        if not self._renderer or not self._renderer.has_texture():
            return QPointF()
        pan = self._transform_controller.get_pan_pixels()
        scale = self._effective_scale()
        world_vec = self._screen_to_world(point)
        tex_vector_x = (world_vec.x() - pan.x()) / scale
        tex_vector_y = (world_vec.y() - pan.y()) / scale
        tex_w, tex_h = self._renderer.texture_size()
        tex_x = tex_w / 2.0 + tex_vector_x
        # Convert the world-space Y (upwards positive) back into image space
        # where increasing values travel down the texture.
        tex_y = tex_h / 2.0 - tex_vector_y
        return QPointF(tex_x, tex_y)

    def _current_crop_rect_pixels(self) -> Optional[dict[str, float]]:
        if not self._renderer or not self._renderer.has_texture():
            return None
        tex_w, tex_h = self._renderer.texture_size()
        rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        top_left = self._image_to_viewport(rect["left"], rect["top"])
        bottom_right = self._image_to_viewport(rect["right"], rect["bottom"])
        dpr = self.devicePixelRatioF()
        return {
            "left": top_left.x() * dpr,
            "top": top_left.y() * dpr,
            "right": bottom_right.x() * dpr,
            "bottom": bottom_right.y() * dpr,
        }

    @staticmethod
    def _distance_to_segment(point: QPointF, start: QPointF, end: QPointF) -> float:
        px, py = point.x(), point.y()
        ax, ay = start.x(), start.y()
        bx, by = end.x(), end.y()
        vx = bx - ax
        vy = by - ay
        if abs(vx) < 1e-6 and abs(vy) < 1e-6:
            return math.hypot(px - ax, py - ay)
        t = ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)
        t = max(0.0, min(1.0, t))
        qx = ax + t * vx
        qy = ay + t * vy
        return math.hypot(px - qx, py - qy)

    def _crop_hit_test(self, point: QPointF) -> CropHandle:
        if not self._renderer or not self._renderer.has_texture():
            return CropHandle.NONE
        tex_w, tex_h = self._renderer.texture_size()
        rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        top_left = self._image_to_viewport(rect["left"], rect["top"])
        top_right = self._image_to_viewport(rect["right"], rect["top"])
        bottom_right = self._image_to_viewport(rect["right"], rect["bottom"])
        bottom_left = self._image_to_viewport(rect["left"], rect["bottom"])

        corners = [
            (CropHandle.TOP_LEFT, top_left),
            (CropHandle.TOP_RIGHT, top_right),
            (CropHandle.BOTTOM_RIGHT, bottom_right),
            (CropHandle.BOTTOM_LEFT, bottom_left),
        ]
        for handle, corner in corners:
            if math.hypot(point.x() - corner.x(), point.y() - corner.y()) <= self._crop_hit_padding:
                return handle

        edges = [
            (CropHandle.TOP, top_left, top_right),
            (CropHandle.RIGHT, top_right, bottom_right),
            (CropHandle.BOTTOM, bottom_left, bottom_right),
            (CropHandle.LEFT, top_left, bottom_left),
        ]
        for handle, start, end in edges:
            if self._distance_to_segment(point, start, end) <= self._crop_hit_padding:
                return handle

        left = min(top_left.x(), bottom_left.x())
        right = max(top_right.x(), bottom_right.x())
        top = min(top_left.y(), top_right.y())
        bottom = max(bottom_left.y(), bottom_right.y())
        if left <= point.x() <= right and top <= point.y() <= bottom:
            return CropHandle.INSIDE
        return CropHandle.NONE

    def _restart_crop_idle(self) -> None:
        if self._crop_mode:
            self._crop_idle_timer.start()

    def _stop_crop_idle(self) -> None:
        self._crop_idle_timer.stop()

    def _stop_crop_animation(self) -> None:
        if self._crop_anim_active:
            self._crop_anim_active = False
            self._crop_anim_timer.stop()

    def _on_crop_idle_timeout(self) -> None:
        self._crop_idle_timer.stop()
        self._start_crop_animation()

    def _start_crop_animation(self) -> None:
        if not self._crop_mode or not self._renderer or not self._renderer.has_texture():
            return
        target_scale = self._target_scale_for_crop()
        tex_w, tex_h = self._renderer.texture_size()
        target_center = self._crop_state.center_pixels(tex_w, tex_h)
        target_center = self._clamp_image_center_to_crop(target_center, target_scale)
        self._crop_anim_active = True
        self._crop_anim_start_time = time.monotonic()
        self._crop_anim_start_scale = self._effective_scale()
        self._crop_anim_target_scale = target_scale
        self._crop_anim_start_center = self._image_center_pixels()
        self._crop_anim_target_center = target_center
        self._crop_anim_timer.start()
        self._crop_faded_out = False

    def _on_crop_anim_tick(self) -> None:
        if not self._crop_anim_active:
            self._crop_anim_timer.stop()
            return
        elapsed = time.monotonic() - self._crop_anim_start_time
        if elapsed >= self._crop_anim_duration:
            scale = self._crop_anim_target_scale
            centre = self._crop_anim_target_center
            self._apply_crop_animation_state(scale, centre)
            self._crop_anim_active = False
            self._crop_anim_timer.stop()
            self._crop_faded_out = True
            self.update()
            return
        progress = max(0.0, min(1.0, elapsed / self._crop_anim_duration))
        eased = ease_out_cubic(progress)
        scale = self._crop_anim_start_scale + (
            (self._crop_anim_target_scale - self._crop_anim_start_scale) * eased
        )
        centre_x = self._crop_anim_start_center.x() + (
            (self._crop_anim_target_center.x() - self._crop_anim_start_center.x()) * eased
        )
        centre_y = self._crop_anim_start_center.y() + (
            (self._crop_anim_target_center.y() - self._crop_anim_start_center.y()) * eased
        )
        self._apply_crop_animation_state(scale, QPointF(centre_x, centre_y))
        self.update()

    def _apply_crop_animation_state(self, scale: float, centre: QPointF) -> None:
        if not self._renderer or not self._renderer.has_texture():
            return
        vw, vh = self._view_dimensions_device_px()
        tex_size = self._renderer.texture_size()
        base_scale = compute_fit_to_view_scale(tex_size, vw, vh)
        min_zoom = self._transform_controller.minimum_zoom()
        max_zoom = self._transform_controller.maximum_zoom()
        zoom_factor = max(min_zoom, min(max_zoom, scale / max(base_scale, 1e-6)))
        self._transform_controller.set_zoom_factor_direct(zoom_factor)
        actual_scale = self._effective_scale()
        clamped_center = self._clamp_image_center_to_crop(centre, actual_scale)
        self._set_image_center_pixels(clamped_center, scale=actual_scale)

    def _target_scale_for_crop(self) -> float:
        if not self._renderer or not self._renderer.has_texture():
            return self._effective_scale()
        tex_w, tex_h = self._renderer.texture_size()
        vw, vh = self._view_dimensions_device_px()
        crop_rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        crop_width = max(1.0, crop_rect["right"] - crop_rect["left"])
        crop_height = max(1.0, crop_rect["bottom"] - crop_rect["top"])
        padding = 20.0 * self.devicePixelRatioF()
        available_w = max(1.0, vw - padding * 2.0)
        available_h = max(1.0, vh - padding * 2.0)
        scale_w = available_w / crop_width
        scale_h = available_h / crop_height
        target_scale = min(scale_w, scale_h)
        base_scale = compute_fit_to_view_scale((tex_w, tex_h), vw, vh)
        min_scale = base_scale * self._transform_controller.minimum_zoom()
        max_scale = base_scale * self._transform_controller.maximum_zoom()
        return max(min_scale, min(max_scale, target_scale))

    def _auto_shrink_on_drag(self, delta: QPointF) -> None:
        if not self._renderer or not self._renderer.has_texture():
            return
        vw, vh = self._view_dimensions_device_px()
        crop_rect = self._current_crop_rect_pixels()
        if crop_rect is None:
            return
        threshold = self._crop_edge_threshold
        left_margin = crop_rect["left"]
        right_margin = vw - crop_rect["right"]
        top_margin = crop_rect["top"]
        bottom_margin = vh - crop_rect["bottom"]
        pressure = 0.0
        delta_x = delta.x()
        delta_y = delta.y()

        if (
            self._crop_drag_handle in (CropHandle.LEFT, CropHandle.TOP_LEFT, CropHandle.BOTTOM_LEFT)
            and delta_x < 0.0
            and left_margin < threshold
        ):
            pressure = max(pressure, (threshold - left_margin) / threshold)
        if (
            self._crop_drag_handle in (CropHandle.RIGHT, CropHandle.TOP_RIGHT, CropHandle.BOTTOM_RIGHT)
            and delta_x > 0.0
            and right_margin < threshold
        ):
            pressure = max(pressure, (threshold - right_margin) / threshold)
        if (
            self._crop_drag_handle in (CropHandle.TOP, CropHandle.TOP_LEFT, CropHandle.TOP_RIGHT)
            and delta_y < 0.0
            and top_margin < threshold
        ):
            pressure = max(pressure, (threshold - top_margin) / threshold)
        if (
            self._crop_drag_handle in (CropHandle.BOTTOM, CropHandle.BOTTOM_LEFT, CropHandle.BOTTOM_RIGHT)
            and delta_y > 0.0
            and bottom_margin < threshold
        ):
            pressure = max(pressure, (threshold - bottom_margin) / threshold)

        if pressure <= 0.0:
            return

        eased = ease_in_quad(min(1.0, pressure))
        current_scale = self._effective_scale()
        tex_size = self._renderer.texture_size()
        vw_float, vh_float = vw, vh
        base_scale = compute_fit_to_view_scale(tex_size, vw_float, vh_float)
        min_scale = base_scale * self._transform_controller.minimum_zoom()
        max_scale = base_scale * self._transform_controller.maximum_zoom()
        new_scale = max(min_scale, min(max_scale, current_scale * (1.0 - 0.05 * eased)))
        anchor = self._crop_center_viewport_point()
        self._transform_controller.set_zoom(new_scale / max(base_scale, 1e-6), anchor=anchor)

        dpr = self.devicePixelRatioF()
        if current_scale <= 1e-6:
            return
        image_delta = QPointF(delta_x * dpr / current_scale, delta_y * dpr / current_scale)

        offset_x = 0.0
        offset_y = 0.0
        if (
            self._crop_drag_handle in (CropHandle.LEFT, CropHandle.TOP_LEFT, CropHandle.BOTTOM_LEFT)
            and delta_x < 0.0
            and left_margin < threshold
        ):
            ratio = (threshold - left_margin) / threshold
            offset_x += -image_delta.x() * ratio
        if (
            self._crop_drag_handle in (CropHandle.RIGHT, CropHandle.TOP_RIGHT, CropHandle.BOTTOM_RIGHT)
            and delta_x > 0.0
            and right_margin < threshold
        ):
            ratio = (threshold - right_margin) / threshold
            offset_x += -image_delta.x() * ratio
        if (
            self._crop_drag_handle in (CropHandle.TOP, CropHandle.TOP_LEFT, CropHandle.TOP_RIGHT)
            and delta_y < 0.0
            and top_margin < threshold
        ):
            ratio = (threshold - top_margin) / threshold
            offset_y += -image_delta.y() * ratio
        if (
            self._crop_drag_handle in (CropHandle.BOTTOM, CropHandle.BOTTOM_LEFT, CropHandle.BOTTOM_RIGHT)
            and delta_y > 0.0
            and bottom_margin < threshold
        ):
            ratio = (threshold - bottom_margin) / threshold
            offset_y += -image_delta.y() * ratio

        pan_gain = 0.75 + 0.25 * eased
        translation = QPointF(offset_x * pan_gain, offset_y * pan_gain)
        if abs(translation.x()) > 1e-4 or abs(translation.y()) > 1e-4:
            tex_w, tex_h = tex_size
            self._crop_state.translate_pixels(translation, tex_size)
            new_center = self._image_center_pixels() + translation
            actual_scale = self._effective_scale()
            clamped = self._clamp_image_center_to_crop(new_center, actual_scale)
            self._set_image_center_pixels(clamped, scale=actual_scale)

    def _emit_crop_changed(self) -> None:
        state = self._crop_state
        self.cropChanged.emit(float(state.cx), float(state.cy), float(state.width), float(state.height))

    def _handle_crop_mouse_press(self, event: QMouseEvent) -> None:
        if not self._renderer or not self._renderer.has_texture():
            return
        self._stop_crop_animation()
        self._stop_crop_idle()
        self._crop_faded_out = False
        pos = event.position()
        handle = self._crop_hit_test(pos)
        if handle == CropHandle.NONE:
            self._crop_drag_handle = CropHandle.NONE
            self._crop_dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        self._crop_drag_handle = handle
        self._crop_dragging = True
        self._crop_last_pos = QPointF(pos)
        if handle == CropHandle.INSIDE:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        else:
            self.setCursor(cursor_for_handle(handle))
        event.accept()

    def _handle_crop_mouse_move(self, event: QMouseEvent) -> None:
        if not self._renderer or not self._renderer.has_texture():
            return
        pos = event.position()
        if not self._crop_dragging:
            handle = self._crop_hit_test(pos)
            self.setCursor(cursor_for_handle(handle))
            return

        previous_pos = QPointF(self._crop_last_pos)
        delta_view = pos - previous_pos
        self._crop_last_pos = QPointF(pos)
        self._crop_faded_out = False

        if self._crop_drag_handle == CropHandle.INSIDE:
            # Convert the pointer delta to texture-space coordinates so the
            # image can track the cursor exactly like ``demo/crop_final.py``.
            # ``_viewport_to_image`` reports positions in image pixels with the
            # conventional top-left origin, therefore subtracting the previous
            # position from the current position yields the motion that the
            # content must follow.  The image centre is translated by the
            # inverse vector, mimicking the "grab image" interaction without
            # ever moving the crop rectangle itself.
            previous_image = self._viewport_to_image(previous_pos)
            current_image = self._viewport_to_image(pos)
            translation = QPointF(
                previous_image.x() - current_image.x(),
                previous_image.y() - current_image.y(),
            )

            if abs(translation.x()) > 1e-6 or abs(translation.y()) > 1e-6:
                centre = self._image_center_pixels()
                new_centre = QPointF(
                    centre.x() + translation.x(),
                    centre.y() + translation.y(),
                )
                actual_scale = self._effective_scale()
                clamped_centre = self._clamp_image_center_to_crop(new_centre, actual_scale)
                self._set_image_center_pixels(clamped_centre, scale=actual_scale)
        else:
            scale = self._effective_scale()
            if scale <= 1e-6:
                return
            dpr = self.devicePixelRatioF()
            image_delta = QPointF(
                delta_view.x() * dpr / scale,
                delta_view.y() * dpr / scale,
            )
            tex_size = self._renderer.texture_size()
            self._crop_state.drag_edge_pixels(self._crop_drag_handle, image_delta, tex_size)
            self._auto_shrink_on_drag(delta_view)
            self._emit_crop_changed()

        self._restart_crop_idle()
        self.update()

    def _handle_crop_mouse_release(self, event: QMouseEvent) -> None:
        del event  # unused
        self._crop_dragging = False
        self._crop_drag_handle = CropHandle.NONE
        self.unsetCursor()
        self._restart_crop_idle()


    # --------------------------- Events ---------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._crop_mode and event.button() == Qt.LeftButton:
            self._handle_crop_mouse_press(event)
            return
        if event.button() == Qt.LeftButton:
            if self._live_replay_enabled:
                self.replayRequested.emit()
            else:
                self._transform_controller.handle_mouse_press(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._crop_mode:
            self._handle_crop_mouse_move(event)
            return
        if not self._live_replay_enabled:
            self._transform_controller.handle_mouse_move(event)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._crop_mode and event.button() == Qt.LeftButton:
            self._handle_crop_mouse_release(event)
            return
        if not self._live_replay_enabled:
            self._transform_controller.handle_mouse_release(event)
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
        if self._crop_mode:
            self._stop_crop_animation()
            self._crop_faded_out = False
            self._stop_crop_idle()
            if not self._renderer or not self._renderer.has_texture():
                return

            angle = event.angleDelta().y()
            if angle == 0:
                self._restart_crop_idle()
                return

            tex_w, tex_h = self._renderer.texture_size()
            vw, vh = self._view_dimensions_device_px()
            base_scale = compute_fit_to_view_scale((tex_w, tex_h), vw, vh)
            if base_scale <= 1e-6:
                return

            # Apply the wheel gesture in the same spirit as ``demo/crop_final.py``:
            # zoom the image content around the pointer while the crop overlay
            # remains stationary.  The dynamic minimum prevents shrinking the
            # texture below the crop rectangle, avoiding any black bars inside
            # the frame.
            crop_rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
            crop_width = max(1.0, crop_rect["right"] - crop_rect["left"])
            crop_height = max(1.0, crop_rect["bottom"] - crop_rect["top"])
            min_zoom_for_crop = max(
                crop_width / max(1.0, float(tex_w)),
                crop_height / max(1.0, float(tex_h)),
            ) / max(base_scale, 1e-6)

            current_zoom = self._transform_controller.get_zoom_factor()
            factor = math.pow(1.0015, angle)
            min_zoom = max(self._transform_controller.minimum_zoom(), min_zoom_for_crop)
            max_zoom = self._transform_controller.maximum_zoom()
            new_zoom = max(min_zoom, min(max_zoom, current_zoom * factor))

            anchor = event.position()
            self._transform_controller.set_zoom(new_zoom, anchor=anchor)

            actual_scale = self._effective_scale()
            centre = self._image_center_pixels()
            clamped_centre = self._clamp_image_center_to_crop(centre, actual_scale)
            self._set_image_center_pixels(clamped_centre, scale=actual_scale)
            self._restart_crop_idle()
            event.accept()
            return
        self._transform_controller.handle_wheel(event)

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

    def _texture_dimensions(self) -> tuple[int, int]:
        """Return the current texture size or ``(0, 0)`` when unavailable."""

        if self._renderer is None:
            return (0, 0)
        return self._renderer.texture_size()

    def _fit_to_view_scale(self, view_width: float, view_height: float) -> float:
        """Return the baseline scale that fits the texture within the viewport."""

        texture_size = self._texture_dimensions()
        return compute_fit_to_view_scale(texture_size, view_width, view_height)

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
