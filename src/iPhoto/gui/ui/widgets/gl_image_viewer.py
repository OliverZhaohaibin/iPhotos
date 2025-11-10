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

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
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

from .crop_overlay import CropOverlay
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
    cropRectChanged = Signal(tuple)
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

        self._display_uv: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
        self._pending_crop_uv: Optional[tuple[float, float, float, float]] = None
        self._crop_overlay = CropOverlay(self)
        self._crop_overlay.hide()
        self._crop_overlay.crop_finished.connect(self._handle_crop_overlay_finished)
        self._crop_overlay_active = False
        self._crop_mode_requested = False

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
        display_uv: tuple[float, float, float, float] | None = None,
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
        display_uv:
            Optional normalised ``(u0, v0, u1, v1)`` rectangle to apply before
            painting.  When omitted the previous UV window is preserved unless
            *reset_view* forces the default framing.
        """

        previous_source = getattr(self, "_current_image_source", None)
        reuse_existing_texture = image_source is not None and image_source == previous_source
        source_changed = image_source != previous_source
        should_reset_view = reset_view or source_changed

        if reuse_existing_texture and image is not None and not image.isNull():
            # Skip the heavy texture re-upload when the caller explicitly
            # reports that the source asset is unchanged.  Only the adjustment
            # uniforms need to be refreshed in this scenario.
            self.set_adjustments(adjustments)
            if display_uv is not None:
                self._set_display_uv(*display_uv)
            elif should_reset_view:
                self._set_display_uv(0.0, 0.0, 1.0, 1.0)
            if should_reset_view:
                self.reset_zoom()
                if self._crop_overlay_active:
                    self._update_crop_overlay_bounds(reset_selection=True)
            return

        self._current_image_source = image_source
        self._image = image
        self._adjustments = dict(adjustments or {})
        self._loading_overlay.hide()
        self._time_base = time.monotonic()

        if display_uv is not None:
            self._set_display_uv(*display_uv)
        elif should_reset_view:
            self._set_display_uv(0.0, 0.0, 1.0, 1.0)

        if image is None or image.isNull():
            self._current_image_source = None
            self.set_crop_mode(False)
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

        if self._crop_overlay_active:
            self._update_crop_overlay_bounds(reset_selection=should_reset_view)

        if self._crop_mode_requested and not self._crop_overlay_active:
            self.set_crop_mode(True)
        elif not self._crop_mode_requested and self._crop_overlay_active:
            self.set_crop_mode(False)

        if should_reset_view:
            # Reset the interactive transform so every new asset begins in the
            # same fit-to-window baseline that the QWidget-based viewer
            # exposes.  ``reset_view`` lets callers preserve the zoom when the
            # user toggles between detail and edit modes.
            self.reset_zoom()
            if self._crop_overlay_active:
                self._update_crop_overlay_bounds(reset_selection=True)
        elif self._crop_overlay_active:
            self._update_crop_overlay_bounds(reset_selection=False)
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

    def set_crop_mode(self, enabled: bool) -> None:
        """Toggle the interactive crop overlay."""

        self._crop_mode_requested = bool(enabled)
        target_state = (
            self._crop_mode_requested
            and self._image is not None
            and not self._image.isNull()
        )
        if target_state == self._crop_overlay_active:
            if target_state:
                self._update_crop_overlay_bounds(reset_selection=False)
            else:
                self._crop_overlay.hide()
            return

        self._crop_overlay_active = target_state
        if target_state:
            self._transform_controller.reset_zoom()
            self._crop_overlay.setVisible(True)
            self._crop_overlay.raise_()
            self._update_crop_overlay_bounds(reset_selection=True)
        else:
            # Exit crop mode: commit pending UV to actually apply the crop
            try:
                self.commit_crop()
            except Exception:
                pass
            self._crop_overlay.hide()

    def is_crop_mode_active(self) -> bool:
        """Return ``True`` when the overlay is accepting crop gestures."""

        return self._crop_overlay_active

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

                display_size = self._displayed_texture_dimensions()
                base_scale = compute_fit_to_view_scale(display_size, float(width), float(height))
                effective_scale = max(base_scale, 1e-6)
                time_value = time.monotonic() - self._time_base
                self._renderer.render(
                    view_width=float(width),
                    view_height=float(height),
                    scale=effective_scale,
                    pan=QPointF(0.0, 0.0),
                    adjustments=dict(adjustments or self._adjustments),
                    time_value=time_value,
                    uv_rect=self._display_uv,
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

        display_size = self._displayed_texture_dimensions()
        base_scale = compute_fit_to_view_scale(display_size, float(vw), float(vh))
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
            uv_rect=self._display_uv,
        )

    # --------------------------- Events ---------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._crop_overlay_active and event.button() == Qt.LeftButton:
            if self._live_replay_enabled:
                self.replayRequested.emit()
            else:
                self._transform_controller.handle_mouse_press(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._crop_overlay_active and not self._live_replay_enabled:
            self._transform_controller.handle_mouse_move(event)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._crop_overlay_active and not self._live_replay_enabled:
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
        if not self._crop_overlay_active:
            self._transform_controller.handle_wheel(event)
        else:
            event.ignore()

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
        if self._crop_overlay is not None:
            self._crop_overlay.setGeometry(self.rect())
            if self._crop_overlay_active:
                self._update_crop_overlay_bounds(reset_selection=False)

    def _texture_dimensions(self) -> tuple[int, int]:
        """Return the current texture size or ``(0, 0)`` when unavailable."""

        return self._displayed_texture_dimensions()

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

    def _displayed_texture_dimensions(self) -> tuple[int, int]:
        """Return the pixel size of the currently visible UV region."""

        tex_w = tex_h = 0
        if self._renderer is not None:
            tex_w, tex_h = self._renderer.texture_size()
        if (tex_w <= 0 or tex_h <= 0) and self._image is not None and not self._image.isNull():
            tex_w, tex_h = self._image.width(), self._image.height()
        if tex_w <= 0 or tex_h <= 0:
            return (0, 0)

        u0, v0, u1, v1 = self._display_uv
        du = max(1e-6, abs(u1 - u0))
        dv = max(1e-6, abs(v1 - v0))
        width_px = max(1, int(round(float(tex_w) * du)))
        height_px = max(1, int(round(float(tex_h) * dv)))
        return (width_px, height_px)

    def _update_crop_overlay_bounds(self, *, reset_selection: bool) -> None:
        overlay = self._crop_overlay
        if overlay is None:
            return
        overlay.setGeometry(self.rect())
        rect01 = self._calculate_overlay_rect_for_current_uv()
        snapped = self._snap_overlay_rect01_to_pixels(rect01)
        overlay.set_bounds_rect(snapped)
        if reset_selection:
            overlay.set_selection_rect(snapped)
        overlay.update()

    def _calculate_overlay_rect_for_current_uv(self) -> QRectF:
        view_w = max(1, self.width())
        view_h = max(1, self.height())
        img_w, img_h = self._displayed_texture_dimensions()
        if img_w <= 0 or img_h <= 0 or view_w <= 0 or view_h <= 0:
            return QRectF(0.0, 0.0, 1.0, 1.0)

        aspect_view = float(view_w) / float(view_h)
        aspect_img = float(img_w) / float(img_h)
        if aspect_img > aspect_view:
            new_h = aspect_view / aspect_img
            y_off = (1.0 - new_h) * 0.5
            return QRectF(0.0, y_off, 1.0, new_h)
        if aspect_img < aspect_view:
            new_w = aspect_img / aspect_view
            x_off = (1.0 - new_w) * 0.5
            return QRectF(x_off, 0.0, new_w, 1.0)
        return QRectF(0.0, 0.0, 1.0, 1.0)

    def _snap_overlay_rect01_to_pixels(self, rect01: QRectF) -> QRectF:
        overlay = self._crop_overlay
        if overlay is None:
            return QRectF(rect01)
        width = max(1, overlay.width())
        height = max(1, overlay.height())
        left = max(0, int(math.floor(rect01.left() * width)))
        top = max(0, int(math.floor(rect01.top() * height)))
        right = min(width, int(math.ceil(rect01.right() * width)))
        bottom = min(height, int(math.ceil(rect01.bottom() * height)))
        if right <= left:
            right = min(width, left + 1)
        if bottom <= top:
            bottom = min(height, top + 1)
        return QRectF(
            float(left) / float(width),
            float(top) / float(height),
            float(right - left) / float(width),
            float(bottom - top) / float(height),
        )

    def _handle_crop_overlay_finished(self, rect01: QRectF) -> None:
        """On release: save pending UV (real crop), then uniform zoom to fill bounds.

        - Keep user's selection shape (don't overwrite it visually).
        - Zoom uniformly so the selection fills the visible container.
        - Avoid stretch: we never modify UV here; only zoom/pan change.
        """
        if not self._crop_overlay_active:
            return

        contain_rect = self._calculate_overlay_rect_for_current_uv()
        if contain_rect.width() <= 0.0 or contain_rect.height() <= 0.0:
            return

        # 1) Record this selection's UV (commit on exit/Done click)
        candidate_uv = self._overlay_rect01_to_uv(rect01)
        if candidate_uv is not None:
            self._pending_crop_uv = candidate_uv

        # 2) Calculate uniform scale needed to fill frame (min rule)
        clipped = rect01.intersected(contain_rect) if not rect01.isEmpty() else contain_rect
        if clipped.isEmpty():
            clipped = contain_rect

        sel_w = max(1e-6, float(clipped.width()))
        sel_h = max(1e-6, float(clipped.height()))
        box_w = max(1e-6, float(contain_rect.width()))
        box_h = max(1e-6, float(contain_rect.height()))
        scale_mult = min(box_w / sel_w, box_h / sel_h)

        current_zoom = self._transform_controller.get_zoom_factor()
        target_zoom = current_zoom * scale_mult

        # --- Key: temporarily raise zoom limit to ensure any aspect ratio can fill ---
        try:
            zmin, zmax = self._transform_controller.get_zoom_limits()
            if target_zoom > zmax + 1e-6:
                # Add margin to avoid floating point edge cases
                self._transform_controller.set_zoom_limits(zmin, target_zoom * 1.05)
        except Exception:
            # Old version without get_zoom_limits, zoom may be limited by original max
            pass

        # 3) Zoom uniformly with selection center as anchor (no UV change, no stretch)
        vw = float(max(1, self.width()))
        vh = float(max(1, self.height()))
        anchor = QPointF(float(clipped.center().x()) * vw, float(clipped.center().y()) * vh)
        self._transform_controller.set_zoom(target_zoom, anchor=anchor)

        # 4) If selection already near center, use translation to center it (not stretch)
        dx = abs(clipped.center().x() - contain_rect.center().x())
        dy = abs(clipped.center().y() - contain_rect.center().y())
        if dx < 0.02 and dy < 0.02 and hasattr(self._transform_controller, "center_on_screen_point"):
            self._transform_controller.center_on_screen_point(anchor)

        # 5) Only update bounds, don't reset selection to full frame (preserve user's aspect ratio)
        if self._crop_overlay is not None:
            self._crop_overlay.set_bounds_rect(contain_rect)
            # Don't call set_selection_rect(contain_rect) - keep user's selection
            self._crop_overlay.update()

        # Note: don't call _set_display_uv, don't reset_zoom; real crop in commit_crop()

    def _set_display_uv(self, u0: float, v0: float, u1: float, v1: float) -> None:
        normalised = self._normalise_uv_rect(u0, v0, u1, v1)
        if normalised == self._display_uv:
            return
        self._display_uv = normalised
        self.cropRectChanged.emit(self._display_uv)
        self.update()

    @staticmethod
    def _normalise_uv_rect(u0: float, v0: float, u1: float, v1: float) -> tuple[float, float, float, float]:
        u_min, u_max = sorted((float(u0), float(u1)))
        v_min, v_max = sorted((float(v0), float(v1)))
        u_min = max(0.0, min(1.0, u_min))
        u_max = max(0.0, min(1.0, u_max))
        v_min = max(0.0, min(1.0, v_min))
        v_max = max(0.0, min(1.0, v_max))
        if abs(u_max - u_min) <= 1e-6:
            u_max = min(1.0, max(0.0, u_min + 1e-6))
        if abs(v_max - v_min) <= 1e-6:
            v_max = min(1.0, max(0.0, v_min + 1e-6))
        return (u_min, v_min, u_max, v_max)

    def _overlay_rect01_to_uv(
        self, sel01: QRectF
    ) -> Optional[tuple[float, float, float, float]]:
        """Map overlay selection (0..1, top-left origin) to UV coords based on current display_uv."""
        contain = self._calculate_overlay_rect_for_current_uv()
        if contain.isEmpty():
            return None
        # Clip user selection to visible container
        clipped = sel01.intersected(contain)
        if clipped.isEmpty():
            return None

        # Normalize to container interior [0,1] (top-left origin)
        cx, cy, cw, ch = contain.left(), contain.top(), contain.width(), contain.height()
        lx0 = (clipped.left() - cx) / max(1e-6, cw)
        lx1 = (clipped.right() - cx) / max(1e-6, cw)
        ly0 = (clipped.top() - cy) / max(1e-6, ch)
        ly1 = (clipped.bottom() - cy) / max(1e-6, ch)

        # Current UV (note: _normalise_uv_rect ensures sorted order)
        u0, v0, u1, v1 = self._display_uv
        u_min, v_min, u_max, v_max = self._normalise_uv_rect(u0, v0, u1, v1)
        du, dv = (u_max - u_min), (v_max - v_min)

        # Composite mapping: horizontal same direction, vertical needs flip
        # because overlay is "top-to-bottom increases" while texture V is usually "bottom-to-top increases"
        new_u0 = u_min + lx0 * du
        new_u1 = u_min + lx1 * du
        new_v0 = v_min + (1.0 - ly1) * dv
        new_v1 = v_min + (1.0 - ly0) * dv

        return self._normalise_uv_rect(new_u0, new_v0, new_u1, new_v1)

    def commit_crop(self) -> bool:
        """Commit pending crop UV and reset to fit-to-view baseline."""
        if not self._pending_crop_uv:
            return False
        u0, v0, u1, v1 = self._pending_crop_uv
        self._pending_crop_uv = None
        self._set_display_uv(u0, v0, u1, v1)  # triggers cropRectChanged
        self.reset_zoom()  # make new crop fit canvas
        if self._crop_overlay_active:
            self._update_crop_overlay_bounds(reset_selection=True)
        return True
