"""
GPU-accelerated image viewer (pure OpenGL texture upload; pixel-accurate zoom/pan).
- Ensures magnification samples the ORIGINAL pixels (no Qt/FBO resampling).
- Uses GL 3.3 Core, VAO/VBO, and a raw glTexImage2D + glTexSubImage2D upload path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from OpenGL import GL as gl
from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPixmap,
    QSurfaceFormat,
    QWheelEvent,
)
from PySide6.QtOpenGL import (
    QOpenGLDebugLogger,
    QOpenGLFunctions_3_3_Core,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel

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

    def __init__(self, parent: QOpenGLWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)

        # 强制 3.3 Core
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)
        self._gl_funcs: QOpenGLFunctions_3_3_Core | None = None
        self._renderer: GLRenderer | None = None
        self._logger: QOpenGLDebugLogger | None = None

        # 状态
        self._image: QImage | None = None
        self._adjustments: dict[str, float] = {}
        self._current_image_source: object | None = None
        self._live_replay_enabled: bool = False

        # Track the viewer surface colour so immersive mode can temporarily
        # switch to a pure black canvas.  ``viewer_surface_color`` returns a
        # palette-derived colour string, which we normalise to ``QColor`` for
        # reliable comparisons and GL clear colour conversion.
        self._default_surface_color = self._normalise_colour(viewer_surface_color(self))
        self._surface_override: QColor | None = None
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
            texture_size_provider=self._texture_dimensions,
            clamp_image_center_to_crop=self._clamp_image_center_to_crop,
            transform_controller=self._transform_controller,
            on_crop_changed=self.cropChanged.emit,
            on_cursor_change=self._handle_cursor_change,
            on_request_update=self.update,
            timer_parent=self,
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
        image: QImage | None,
        shader_adjustments: Mapping[str, float] | None = None,
        *,
        raw_adjustments_for_reset: Mapping[str, float] | None = None,
        image_source: object | None = None,
        reset_view: bool = True,
    ) -> None:
        """Display *image* together with optional colour *adjustments*.

        Parameters
        ----------
        image:
            ``QImage`` backing the GL texture. ``None`` clears the viewer.
        shader_adjustments:
            Mapping of Photos-style adjustment values (resolved) to apply in the shader.
        raw_adjustments_for_reset:
            Mapping of raw adjustment values (from sidecar/session) used for
            view reset logic (e.g., zoom-to-crop).
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
            self.set_adjustments(shader_adjustments)
            
            # Update crop controller with saved crop parameters for rendering
            self._crop_controller.set_crop_transform_values(raw_adjustments_for_reset)
            
            if reset_view:
                # Pass the current image size to avoid race conditions
                img_size = image.size() if image is not None else None
                self.reset_zoom_to_crop(raw_adjustments_for_reset, new_image_size=img_size)
            return

        self._current_image_source = image_source
        self._image = image
        self._adjustments = dict(shader_adjustments or {})
        self._loading_overlay.hide()
        self._time_base = time.monotonic()
        
        # Update crop controller with saved crop parameters for rendering
        self._crop_controller.set_crop_transform_values(raw_adjustments_for_reset)

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
            # Use crop-aware reset if crop parameters exist in raw adjustments.
            # Pass the new image size to avoid race conditions with texture upload.
            img_size = image.size() if image is not None and not image.isNull() else None
            self.reset_zoom_to_crop(raw_adjustments_for_reset, new_image_size=img_size)
            
    def set_placeholder(self, pixmap) -> None:
        """Display *pixmap* without changing the tracked image source."""

        if pixmap and not pixmap.isNull():
            self.set_image(
                pixmap.toImage(),
                {},
                raw_adjustments_for_reset=None,
                image_source=self._current_image_source,
            )
        else:
            self.set_image(None, {}, raw_adjustments_for_reset=None, image_source=None)

    def set_pixmap(
        self,
        pixmap: QPixmap | None,
        image_source: object | None = None,
        *,
        reset_view: bool = True,
    ) -> None:
        """Compatibility wrapper mirroring :class:`ImageViewer`.

        The optional *image_source* is forwarded to :meth:`set_image` so callers
        can keep the existing texture alive when reusing the same asset.
        """

        if pixmap is None or pixmap.isNull():
            self.set_image(
                None,
                {},
                raw_adjustments_for_reset=None,
                image_source=None,
                reset_view=reset_view,
            )
            return
        self.set_image(
            pixmap.toImage(),
            {},
            raw_adjustments_for_reset=None,
            image_source=image_source if image_source is not None else self._current_image_source,
            reset_view=reset_view,
        )

    def clear(self) -> None:
        """Reset the viewer to an empty state."""

        self.set_image(None, {}, raw_adjustments_for_reset=None, image_source=None)

    def set_adjustments(self, adjustments: Mapping[str, float] | None = None) -> None:
        """Update the active adjustment uniforms without replacing the texture."""

        mapped_adjustments = dict(adjustments or {})
        self._adjustments = mapped_adjustments
        self.update()

    def current_image_source(self) -> object | None:
        """Return the identifier describing the currently displayed image."""

        return getattr(self, "_current_image_source", None)

    def pixmap(self) -> QPixmap | None:
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

    def viewport_widget(self) -> GLImageViewer:
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

    def set_zoom(self, factor: float, anchor: QPointF | None = None) -> None:
        """Adjust the zoom while preserving the requested *anchor* pixel."""

        anchor_point = anchor or self.viewport_center()
        self._transform_controller.set_zoom(float(factor), anchor_point)

    def reset_zoom(self) -> None:
        self._transform_controller.reset_zoom()

    def reset_zoom_to_crop(
        self,
        adjustments: Mapping[str, float] | None = None,
        *,
        new_image_size: QSize | None = None,
    ) -> None:
        """Reset zoom and pan to focus on the cropped region if crop parameters exist.
        
        Parameters
        ----------
        adjustments:
            Mapping containing adjustment values including crop parameters.
            If crop parameters indicate no actual crop or are missing, falls back
            to standard reset_zoom() behavior.
        new_image_size:
            Optional QSize of the new image being loaded. If provided, this size
            will be used for calculations instead of the current GPU texture size.
            This avoids race conditions when the image hasn't been uploaded yet.
        """
        if not adjustments:
            self.reset_zoom()
            return
        
        # Extract crop parameters
        crop_params = {
            "Crop_CX": adjustments.get("Crop_CX", 0.5),
            "Crop_CY": adjustments.get("Crop_CY", 0.5),
            "Crop_W": adjustments.get("Crop_W", 1.0),
            "Crop_H": adjustments.get("Crop_H", 1.0),
        }
        
        self._transform_controller.reset_zoom_to_rect(crop_params, new_image_size=new_image_size)

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
        adjustments: Mapping[str, float] | None = None,
    ) -> QImage:
        """Render the current texture into an off-screen framebuffer.

        Parameters
        ----------
        target_size:
            Final size of the rendered preview.
        adjustments:
            Mapping of shader uniform values to apply during rendering.  Passing
            ``None`` renders the frame using the viewer's current adjustment state.

        Returns
        -------
        QImage
            CPU-side image containing the rendered frame. The image is always
            in Format_ARGB32 for downstream consumers.

        Notes
        -----
        The width and height of the rendered image are clamped to at least one pixel
        to avoid driver errors. The returned image is always in Format_ARGB32 format.
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

        if self._renderer is None:
            _LOGGER.warning("render_offscreen_image: renderer not initialized")
            return QImage()

        self.makeCurrent()
        try:
            return self._renderer.render_offscreen_image(
                self._image,
                adjustments or self._adjustments,
                target_size,
                time_base=self._time_base,
            )
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
        
        # Always get crop transforms from controller
        # The controller now stores correct values even when not in active crop mode
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

    def setCropMode(self, enabled: bool, values: Mapping[str, float] | None = None) -> None:
        self._crop_controller.set_active(enabled, values)

    def crop_values(self) -> dict[str, float]:
        return self._crop_controller.get_crop_values()

    # --------------------------- Coordinate transformations ---------------------------

    def _view_dimensions_device_px(self) -> tuple[float, float]:
        return self._transform_controller._get_view_dimensions_device_px()

    def _screen_to_world(self, screen_pt: QPointF) -> QPointF:
        """Map a Qt screen coordinate to the GL view's centre-origin space."""
        return self._transform_controller.convert_screen_to_world(screen_pt)

    def _world_to_screen(self, world_vec: QPointF) -> QPointF:
        """Convert a GL centre-origin vector into a Qt screen coordinate."""
        return self._transform_controller.convert_world_to_screen(world_vec)

    def _effective_scale(self) -> float:
        if not self._renderer or not self._renderer.has_texture():
            return 1.0
        return self._transform_controller.get_effective_scale()

    def _image_center_pixels(self) -> QPointF:
        if not self._renderer or not self._renderer.has_texture():
            return QPointF(0.0, 0.0)
        return self._transform_controller.get_image_center_pixels()

    def _set_image_center_pixels(self, center: QPointF, *, scale: float | None = None) -> None:
        if not self._renderer or not self._renderer.has_texture():
            return
        self._transform_controller.apply_image_center_pixels(center, scale)

    def _image_to_viewport(self, x: float, y: float) -> QPointF:
        if not self._renderer or not self._renderer.has_texture():
            return QPointF()
        return self._transform_controller.convert_image_to_viewport(x, y)

    def _viewport_to_image(self, point: QPointF) -> QPointF:
        if not self._renderer or not self._renderer.has_texture():
            return QPointF()
        return self._transform_controller.convert_viewport_to_image(point)

    def _clamp_image_center_to_crop(self, center: QPointF, scale: float) -> QPointF:
        """Return *center* limited so the viewport never exposes empty pixels.

        The demo reference keeps the camera free to roam while the crop-specific
        model transform guarantees that the crop rectangle only samples valid
        pixels.  To mirror that behaviour we clamp the *viewport* centre purely
        against the texture bounds.  As soon as the viewport half extents exceed
        the texture half extents we collapse the permissible interval to the
        image midpoint, matching the demo's behaviour once the frame is larger
        than the source.
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

        tex_half_w = float(tex_w) * 0.5
        tex_half_h = float(tex_h) * 0.5

        min_center_x = half_view_w
        max_center_x = float(tex_w) - half_view_w

        if min_center_x > max_center_x:
            min_center_x = tex_half_w
            max_center_x = tex_half_w

        min_center_y = half_view_h
        max_center_y = float(tex_h) - half_view_h

        if min_center_y > max_center_y:
            min_center_y = tex_half_h
            max_center_y = tex_half_h

        clamped_x = max(min_center_x, min(max_center_x, float(center.x())))
        clamped_y = max(min_center_y, min(max_center_y, float(center.y())))

        clamped_x = max(0.0, min(float(tex_w), clamped_x))
        clamped_y = max(0.0, min(float(tex_h), clamped_y))
        return QPointF(clamped_x, clamped_y)

    # --------------------------- Viewport helpers ---------------------------

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

    # --------------------------- Cursor management and helpers ---------------------------

    def _handle_cursor_change(self, cursor: Qt.CursorShape | None) -> None:
        """Handle cursor change request from controllers."""
        if cursor is None:
            self.unsetCursor()
        else:
            self.setCursor(cursor)

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
