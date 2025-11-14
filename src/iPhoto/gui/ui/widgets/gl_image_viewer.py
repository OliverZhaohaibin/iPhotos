# -*- coding: utf-8 -*-
"""
GPU-accelerated image viewer (pure OpenGL texture upload; pixel-accurate zoom/pan).
- Ensures magnification samples the ORIGINAL pixels (no Qt/FBO resampling).
- Uses GL 3.3 Core, VAO/VBO, and a raw glTexImage2D + glTexSubImage2D upload path.
"""

from __future__ import annotations

from typing import Mapping, Optional

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

from .gl_crop_utils import (
    CropBoxState,
    CropHandle,
    cursor_for_handle,
    ease_in_quad,
    ease_out_cubic,
)
from .gl_crop_controller import CropInteractionController
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

        # Crop interaction controller
        self._crop_controller = CropInteractionController(
            self,
            texture_size_provider=self._texture_dimensions,
            view_dimensions_provider=self._view_dimensions_device_px,
            effective_scale_provider=self._effective_scale,
            image_center_provider=self._image_center_pixels,
            set_image_center=self._set_image_center_pixels_internal,
            clamp_image_center_to_crop=self._clamp_image_center_to_crop,
            image_to_viewport=self._image_to_viewport,
            viewport_to_image=self._viewport_to_image,
            screen_to_world=self._screen_to_world,
            transform_controller=self._transform_controller,
            on_crop_changed=self.cropChanged.emit,
        )

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
        
        view_pan = self._transform_controller.get_pan_pixels()
        img_scale = 1.0
        img_offset = QPointF(0.0, 0.0)
        if self._crop_controller.is_active():
            img_offset, img_scale = self._crop_controller.get_crop_model_transform()

        self._renderer.render(
            view_width=float(vw),
            view_height=float(vh),
            scale=effective_scale,
            pan=view_pan,
            adjustments=self._adjustments,
            time_value=time_value,
            img_scale=img_scale,
            img_offset=img_offset,
        )

        if self._crop_controller.is_active():
            crop_rect = self._crop_controller.current_crop_rect_pixels()
            if crop_rect is not None:
                self._renderer.draw_crop_overlay(
                    view_width=float(vw),
                    view_height=float(vh),
                    crop_rect=crop_rect,
                    faded=self._crop_controller.is_faded_out(),
                )

    # --------------------------- Crop helpers ---------------------------

    def setCropMode(self, enabled: bool, values: Optional[Mapping[str, float]] = None) -> None:
        self._crop_controller.set_active(enabled, values)

    def crop_values(self) -> dict[str, float]:
        return self._crop_controller.get_crop_values()

    # --------------------------- Coordinate transformations ---------------------------
        dpr = self.devicePixelRatioF()
        vw = max(1.0, float(self.width()) * dpr)
        vh = max(1.0, float(self.height()) * dpr)
        return vw, vh

    def _screen_to_world(self, screen_pt: QPointF) -> QPointF:
        """Map a Qt screen coordinate to the GL view's centre-origin space."""
        dpr = self.devicePixelRatioF()
        vw, vh = self._view_dimensions_device_px()
        return self._transform_controller.screen_to_world(
            screen_pt, float(self.width()), float(self.height()), dpr
        )

    def _world_to_screen(self, world_vec: QPointF) -> QPointF:
        """Convert a GL centre-origin vector into a Qt screen coordinate."""
        dpr = self.devicePixelRatioF()
        return self._transform_controller.world_to_screen(
            world_vec, float(self.width()), float(self.height()), dpr
        )

    def _effective_scale(self) -> float:
        if not self._renderer or not self._renderer.has_texture():
            return 1.0
        vw, vh = self._view_dimensions_device_px()
        return self._transform_controller.effective_scale(
            self._renderer.texture_size(), vw, vh
        )

    def _image_center_pixels(self) -> QPointF:
        if not self._renderer or not self._renderer.has_texture():
            return QPointF(0.0, 0.0)
        scale = self._effective_scale()
        return self._transform_controller.image_center_pixels(
            self._renderer.texture_size(), scale
        )

    def _set_image_center_pixels(self, center: QPointF, *, scale: float | None = None) -> None:
        if not self._renderer or not self._renderer.has_texture():
            return
        scale_value = scale if scale is not None else self._effective_scale()
        self._transform_controller.set_image_center_pixels(
            center, self._renderer.texture_size(), scale_value
        )

    def _set_image_center_pixels_internal(self, center: QPointF, scale: float) -> None:
        """Internal helper for the crop controller to set image center."""
        if not self._renderer or not self._renderer.has_texture():
            return
        self._transform_controller.set_image_center_pixels(
            center, self._renderer.texture_size(), scale
        )

    def _clamp_image_center_to_crop(self, center: QPointF, scale: float) -> QPointF:
        """Return *center* limited so the crop box always sees valid pixels.

        The permissible range is derived from the portion of the texture that
        must remain visible *inside the crop overlay*.  Unlike the legacy
        implementation—which forced the whole viewport to stay within the
        texture—this formulation mirrors ``demo/crop_final.py`` and allows the
        image to travel freely until a crop edge would reveal empty space.  The
        calculation works in image-space pixels and therefore plays nicely with
        the normalised crop state without introducing additional coordinate
        transforms.

        ``scale`` represents the number of device pixels per image pixel.  It
        tells us how many texture pixels are required to fill the viewport and
        consequently how far the image centre may move before the crop would
        overrun the actual texture boundaries.
        """

        if (
            not self._renderer
            or not self._renderer.has_texture()
            or scale <= 1e-9
        ):
            return center

        tex_w, tex_h = self._renderer.texture_size()
        vw, vh = self._view_dimensions_device_px()

        half_view_w = (float(vw) / float(scale)) * 0.5
        half_view_h = (float(vh) / float(scale)) * 0.5

        crop_rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        crop_left = float(crop_rect["left"])
        crop_top = float(crop_rect["top"])
        crop_right = float(crop_rect["right"])
        crop_bottom = float(crop_rect["bottom"])

        min_center_x = crop_right - half_view_w
        max_center_x = crop_left + half_view_w
        min_center_y = crop_bottom - half_view_h
        max_center_y = crop_top + half_view_h

        min_center_x = max(0.0, min_center_x)
        max_center_x = min(float(tex_w), max_center_x)
        min_center_y = max(0.0, min_center_y)
        max_center_y = min(float(tex_h), max_center_y)

        if min_center_x > max_center_x:
            crop_centre_x = (crop_left + crop_right) * 0.5
            clamped = max(0.0, min(float(tex_w), crop_centre_x))
            min_center_x = clamped
            max_center_x = clamped
        if min_center_y > max_center_y:
            crop_centre_y = (crop_top + crop_bottom) * 0.5
            clamped = max(0.0, min(float(tex_h), crop_centre_y))
            min_center_y = clamped
            max_center_y = clamped

        clamped_x = max(min_center_x, min(max_center_x, float(center.x())))
        clamped_y = max(min_center_y, min(max_center_y, float(center.y())))
        return QPointF(clamped_x, clamped_y)

    # --------------------------- Events ---------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._crop_controller.is_active() and event.button() == Qt.LeftButton:
            self._crop_controller.handle_mouse_press(event)
            return
        if event.button() == Qt.LeftButton:
            if self._live_replay_enabled:
                self.replayRequested.emit()
            else:
                self._transform_controller.handle_mouse_press(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._crop_controller.is_active():
            self._crop_controller.handle_mouse_move(event)
            return
        if not self._live_replay_enabled:
            self._transform_controller.handle_mouse_move(event)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._crop_controller.is_active() and event.button() == Qt.LeftButton:
            self._crop_controller.handle_mouse_release(event)
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
        if self._crop_controller.is_active():
            self._crop_controller.handle_wheel(event)
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
