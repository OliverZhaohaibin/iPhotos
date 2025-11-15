"""Utilities for handling zoom and pan interaction in the GL image viewer."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget


def compute_fit_to_view_scale(
    texture_size: tuple[int, int],
    view_width: float,
    view_height: float,
) -> float:
    """Return the scale that fits *texture_size* inside the viewport dimensions."""

    tex_w, tex_h = texture_size
    if tex_w <= 0 or tex_h <= 0:
        return 1.0
    if view_width <= 0.0 or view_height <= 0.0:
        return 1.0
    width_ratio = view_width / float(tex_w)
    height_ratio = view_height / float(tex_h)
    scale = min(width_ratio, height_ratio)
    return 1.0 if scale <= 0.0 else scale


class ViewTransformController:
    """Maintain zoom/pan state and react to pointer gestures."""

    def __init__(
        self,
        viewer: QOpenGLWidget,
        *,
        texture_size_provider: Callable[[], tuple[int, int]],
        on_zoom_changed: Callable[[float], None],
        on_next_item: Callable[[], None] | None = None,
        on_prev_item: Callable[[], None] | None = None,
    ) -> None:
        self._viewer = viewer
        self._texture_size_provider = texture_size_provider
        self._on_zoom_changed = on_zoom_changed
        self._on_next_item = on_next_item
        self._on_prev_item = on_prev_item

        self._zoom_factor: float = 1.0
        self._min_zoom: float = 0.1
        self._max_zoom: float = 16.0
        self._pan_px: QPointF = QPointF(0.0, 0.0)
        self._is_panning: bool = False
        self._pan_start_pos: QPointF = QPointF()
        self._wheel_action: str = "zoom"

    # ------------------------------------------------------------------
    # Helper methods for getting viewport info
    # ------------------------------------------------------------------
    def _get_view_dimensions_device_px(self) -> tuple[float, float]:
        """Get viewport dimensions in device pixels."""
        dpr = self._viewer.devicePixelRatioF()
        vw = max(1.0, float(self._viewer.width()) * dpr)
        vh = max(1.0, float(self._viewer.height()) * dpr)
        return vw, vh
    
    def _get_dpr(self) -> float:
        """Get device pixel ratio."""
        return self._viewer.devicePixelRatioF()

    def _get_view_dimensions_logical(self) -> tuple[float, float]:
        """Get viewport dimensions in logical pixels."""
        return float(self._viewer.width()), float(self._viewer.height())

    # ------------------------------------------------------------------
    # Public wrappers for viewport info
    # ------------------------------------------------------------------
    def get_view_dimensions_device_px(self) -> tuple[float, float]:
        """Public wrapper for device pixel viewport dimensions."""
        return self._get_view_dimensions_device_px()

    def get_dpr(self) -> float:
        """Public wrapper for device pixel ratio."""
        return self._get_dpr()

    def get_view_dimensions_logical(self) -> tuple[float, float]:
        """Public wrapper for logical pixel viewport dimensions."""
        return self._get_view_dimensions_logical()

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------
    def get_zoom_factor(self) -> float:
        return self._zoom_factor

    def get_pan_pixels(self) -> QPointF:
        return QPointF(self._pan_px)

    def set_pan_pixels(self, pan: QPointF) -> None:
        self._pan_px = QPointF(pan)
        self._viewer.update()

    def minimum_zoom(self) -> float:
        return self._min_zoom

    def maximum_zoom(self) -> float:
        return self._max_zoom

    def set_zoom_factor_direct(self, factor: float) -> None:
        clamped = max(self._min_zoom, min(self._max_zoom, float(factor)))
        if abs(clamped - self._zoom_factor) < 1e-6:
            return
        self._zoom_factor = clamped
        self._viewer.update()
        self._on_zoom_changed(self._zoom_factor)

    def set_zoom_limits(self, minimum: float, maximum: float) -> None:
        """Clamp the interactive zoom range."""

        self._min_zoom = float(minimum)
        self._max_zoom = float(maximum)

    def set_wheel_action(self, action: str) -> None:
        """Switch between wheel zooming and item navigation."""

        self._wheel_action = "zoom" if action == "zoom" else "navigate"

    # ------------------------------------------------------------------
    # Zoom utilities
    # ------------------------------------------------------------------
    def set_zoom(self, factor: float, anchor: QPointF | None = None) -> bool:
        """Update the zoom factor while keeping *anchor* stationary."""

        clamped = max(self._min_zoom, min(self._max_zoom, float(factor)))
        if abs(clamped - self._zoom_factor) < 1e-6:
            return False

        anchor_point = anchor or QPointF(self._viewer.width() / 2, self._viewer.height() / 2)
        tex_w, tex_h = self._texture_size_provider()
        if (
            anchor_point is not None
            and tex_w > 0
            and tex_h > 0
            and self._viewer.width() > 0
            and self._viewer.height() > 0
        ):
            dpr = self._viewer.devicePixelRatioF()
            view_width = float(self._viewer.width()) * dpr
            view_height = float(self._viewer.height()) * dpr
            base_scale = compute_fit_to_view_scale((tex_w, tex_h), view_width, view_height)
            old_scale = base_scale * self._zoom_factor
            new_scale = base_scale * clamped
            if old_scale > 1e-6 and new_scale > 0.0:
                anchor_bottom_left = QPointF(
                    anchor_point.x() * dpr, view_height - anchor_point.y() * dpr
                )
                view_centre = QPointF(view_width / 2.0, view_height / 2.0)
                anchor_vector = anchor_bottom_left - view_centre
                tex_coord_x = (anchor_vector.x() - self._pan_px.x()) / old_scale
                tex_coord_y = (anchor_vector.y() - self._pan_px.y()) / old_scale
                self._pan_px = QPointF(
                    anchor_vector.x() - tex_coord_x * new_scale,
                    anchor_vector.y() - tex_coord_y * new_scale,
                )

        self._zoom_factor = clamped
        self._viewer.update()
        self._on_zoom_changed(self._zoom_factor)
        return True

    def reset_zoom(self) -> bool:
        """Restore the baseline zoom and recenter the texture."""

        changed = (
            abs(self._zoom_factor - 1.0) > 1e-6
            or abs(self._pan_px.x()) > 1e-6
            or abs(self._pan_px.y()) > 1e-6
        )
        self._zoom_factor = 1.0
        self._pan_px = QPointF(0.0, 0.0)
        self._viewer.update()
        self._on_zoom_changed(self._zoom_factor)
        return changed

    def reset_zoom_to_rect(
        self,
        crop_params: Mapping[str, float],
        *,
        new_image_size: QSize | None = None,
    ) -> bool:
        """Reset zoom and pan to fit the specified crop rectangle to the viewport.
        
        Parameters
        ----------
        crop_params:
            Mapping containing normalized crop values (Crop_CX, Crop_CY, Crop_W, Crop_H).
            All values are expected to be in [0.0, 1.0] range where:
            - Crop_CX, Crop_CY: normalized center coordinates
            - Crop_W, Crop_H: normalized width/height (1.0 = full dimension)
        new_image_size:
            Optional QSize of the new image being loaded. If provided, this size
            will be used instead of querying the (potentially stale) GPU texture size.
            This avoids race conditions when set_image is called but the texture
            hasn't been uploaded to GPU yet.
            
        Returns
        -------
        bool
            True if the view transform was changed, False otherwise.
            
        Notes
        -----
        If crop parameters indicate no actual crop (Crop_W and Crop_H are 1.0),
        this method falls back to the standard reset_zoom() behavior.
        """
        crop_w = float(crop_params.get("Crop_W", 1.0))
        crop_h = float(crop_params.get("Crop_H", 1.0))
        
        # If no actual crop is applied, use standard reset
        if abs(crop_w - 1.0) < 1e-6 and abs(crop_h - 1.0) < 1e-6:
            return self.reset_zoom()
        
        # Get full texture dimensions - prefer new_image_size if provided
        if new_image_size is not None and not new_image_size.isEmpty():
            tex_w = new_image_size.width()
            tex_h = new_image_size.height()
        else:
            tex_w, tex_h = self._texture_size_provider()
            
        if tex_w <= 0 or tex_h <= 0:
            return self.reset_zoom()
        
        # Calculate crop region in pixels
        crop_pixel_w = float(tex_w) * crop_w
        crop_pixel_h = float(tex_h) * crop_h
        
        if crop_pixel_w <= 0.0 or crop_pixel_h <= 0.0:
            return self.reset_zoom()
        
        # Get viewport dimensions in device pixels
        vw, vh = self._get_view_dimensions_device_px()
        
        # Calculate the base scale needed to fit the crop region to the viewport
        # This is similar to compute_fit_to_view_scale but for the crop region
        base_scale = min(vw / crop_pixel_w, vh / crop_pixel_h)
        
        # Store old values to determine if anything changed
        old_zoom = self._zoom_factor
        old_pan = QPointF(self._pan_px)
        
        # Set zoom factor to 1.0 - the base_scale already represents "fit to crop"
        self._zoom_factor = 1.0
        
        # Calculate the effective scale (base_scale already fits crop region)
        effective_scale = base_scale * self._zoom_factor
        
        # Calculate crop center in pixel coordinates
        crop_cx = float(crop_params.get("Crop_CX", 0.5))
        crop_cy = float(crop_params.get("Crop_CY", 0.5))
        crop_center_x = float(tex_w) * crop_cx
        crop_center_y = float(tex_h) * crop_cy
        
        # Set the view center to the crop center using the effective scale
        crop_center = QPointF(crop_center_x, crop_center_y)
        self.set_image_center_pixels(crop_center, (tex_w, tex_h), effective_scale)
        
        # Notify about zoom change
        self._viewer.update()
        self._on_zoom_changed(self._zoom_factor)
        
        # Determine if anything actually changed
        changed = (
            abs(old_zoom - self._zoom_factor) > 1e-6
            or abs(old_pan.x() - self._pan_px.x()) > 1e-6
            or abs(old_pan.y() - self._pan_px.y()) > 1e-6
        )
        return changed

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def handle_mouse_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_panning = True
            self._pan_start_pos = event.position()
            self._viewer.setCursor(Qt.CursorShape.ClosedHandCursor)

    def handle_mouse_move(self, event: QMouseEvent) -> None:
        if not self._is_panning:
            return
        delta = event.position() - self._pan_start_pos
        self._pan_start_pos = event.position()
        dpr = self._viewer.devicePixelRatioF()
        self._pan_px += QPointF(delta.x() * dpr, -delta.y() * dpr)
        self._viewer.update()

    def handle_mouse_release(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_panning = False
            self._viewer.unsetCursor()

    def handle_wheel(self, event: QWheelEvent) -> None:
        if self._wheel_action == "zoom":
            angle = event.angleDelta().y()
            if angle > 0:
                self.set_zoom(self._zoom_factor * 1.1, anchor=event.position())
            elif angle < 0:
                self.set_zoom(self._zoom_factor / 1.1, anchor=event.position())
        else:
            delta = event.angleDelta()
            step = delta.y() or delta.x()
            if step < 0 and self._on_next_item is not None:
                self._on_next_item()
            elif step > 0 and self._on_prev_item is not None:
                self._on_prev_item()
        event.accept()

    # ------------------------------------------------------------------
    # Coordinate transformation utilities
    # ------------------------------------------------------------------
    def screen_to_world(
        self, screen_pt: QPointF, view_width: float, view_height: float, dpr: float
    ) -> QPointF:
        """Map a Qt screen coordinate to the GL view's centre-origin space.

        Qt reports positions in logical pixels with the origin at the top-left and
        a downward pointing Y axis.  The renderer however reasons about vectors in
        device pixels where the origin lives at the viewport centre and the Y axis
        grows upwards.  This helper performs the origin shift, the device pixel
        conversion and the Y flip so every caller receives a world-space vector
        that matches what the shader expects.
        """
        vw = view_width * dpr
        vh = view_height * dpr
        sx = float(screen_pt.x()) * dpr
        sy = float(screen_pt.y()) * dpr
        world_x = sx - (vw * 0.5)
        world_y = (vh * 0.5) - sy
        return QPointF(world_x, world_y)

    def world_to_screen(
        self, world_vec: QPointF, view_width: float, view_height: float, dpr: float
    ) -> QPointF:
        """Convert a GL centre-origin vector into a Qt screen coordinate.

        The inverse of :meth:`screen_to_world`: a world vector expressed in
        pixels relative to the viewport centre (Y up) is translated back into the
        top-left origin, Y-down coordinate system that Qt painting routines use.
        The return value is expressed in logical pixels to remain consistent with
        Qt's high-DPI handling.
        """
        vw = view_width * dpr
        vh = view_height * dpr
        sx = float(world_vec.x()) + (vw * 0.5)
        sy = (vh * 0.5) - float(world_vec.y())
        return QPointF(sx / dpr, sy / dpr)

    def effective_scale(
        self, texture_size: tuple[int, int], view_width: float, view_height: float
    ) -> float:
        """Calculate the effective rendering scale (base scale × zoom factor)."""
        base_scale = compute_fit_to_view_scale(texture_size, view_width, view_height)
        return max(base_scale * self._zoom_factor, 1e-6)

    def image_center_pixels(
        self, texture_size: tuple[int, int], scale: float
    ) -> QPointF:
        """Return the image center in pixel coordinates based on current pan/zoom."""
        tex_w, tex_h = texture_size
        centre_x = (tex_w / 2.0) - (self._pan_px.x() / scale)
        # ``pan.y`` grows upwards in world space, therefore the corresponding
        # image coordinate moves towards the bottom (larger Y values) in the
        # conventional top-left origin texture space.
        centre_y = (tex_h / 2.0) + (self._pan_px.y() / scale)
        return QPointF(centre_x, centre_y)

    def set_image_center_pixels(
        self, center: QPointF, texture_size: tuple[int, int], scale: float
    ) -> None:
        """Set the pan to center the image at the given pixel coordinate."""
        tex_w, tex_h = texture_size
        delta_x = center.x() - (tex_w / 2.0)
        delta_y = center.y() - (tex_h / 2.0)
        # ``delta_y`` measures how far the requested centre sits below the image
        # mid-line; in world space that translates to a positive upward pan.
        pan = QPointF(-delta_x * scale, delta_y * scale)
        self.set_pan_pixels(pan)

    def image_to_viewport(
        self,
        x: float,
        y: float,
        texture_size: tuple[int, int],
        scale: float,
        view_width: float,
        view_height: float,
        dpr: float,
    ) -> QPointF:
        """Convert image pixel coordinates to viewport coordinates."""
        tex_w, tex_h = texture_size
        tex_vector_x = x - (tex_w / 2.0)
        tex_vector_y = y - (tex_h / 2.0)
        world_vector = QPointF(
            tex_vector_x * scale + self._pan_px.x(),
            -(tex_vector_y * scale) + self._pan_px.y(),
        )
        # ``world_vector`` is now expressed in the GL-friendly centre-origin
        # space, so the last step is to convert it back to Qt's screen space.
        return self.world_to_screen(world_vector, view_width, view_height, dpr)

    def viewport_to_image(
        self,
        point: QPointF,
        texture_size: tuple[int, int],
        scale: float,
        view_width: float,
        view_height: float,
        dpr: float,
    ) -> QPointF:
        """Convert viewport coordinates to image pixel coordinates."""
        world_vec = self.screen_to_world(point, view_width, view_height, dpr)
        tex_vector_x = (world_vec.x() - self._pan_px.x()) / scale
        tex_vector_y = (world_vec.y() - self._pan_px.y()) / scale
        tex_w, tex_h = texture_size
        tex_x = tex_w / 2.0 + tex_vector_x
        # Convert the world-space Y (upwards positive) back into image space
        # where increasing values travel down the texture.
        tex_y = tex_h / 2.0 - tex_vector_y
        return QPointF(tex_x, tex_y)

    # ------------------------------------------------------------------
    # Convenience methods that use internal viewport state
    # ------------------------------------------------------------------
    def get_effective_scale(self) -> float:
        """Calculate the effective rendering scale using internal viewport state."""
        vw, vh = self._get_view_dimensions_device_px()
        texture_size = self._texture_size_provider()
        return self.effective_scale(texture_size, vw, vh)
    
    def get_image_center_pixels(self) -> QPointF:
        """Return the image center in pixel coordinates using current pan/zoom."""
        texture_size = self._texture_size_provider()
        scale = self.get_effective_scale()
        return self.image_center_pixels(texture_size, scale)
    
    def apply_image_center_pixels(self, center: QPointF, scale: float | None = None) -> None:
        """Set the pan to center the image at the given pixel coordinate."""
        texture_size = self._texture_size_provider()
        scale_value = scale if scale is not None else self.get_effective_scale()
        self.set_image_center_pixels(center, texture_size, scale_value)
    
    def convert_screen_to_world(self, screen_pt: QPointF) -> QPointF:
        """Map a Qt screen coordinate to GL view's centre-origin space."""
        view_width, view_height = self._get_view_dimensions_logical()
        dpr = self._get_dpr()
        return self.screen_to_world(screen_pt, view_width, view_height, dpr)
    
    def convert_world_to_screen(self, world_vec: QPointF) -> QPointF:
        """Convert a GL centre-origin vector into a Qt screen coordinate."""
        view_width, view_height = self._get_view_dimensions_logical()
        dpr = self._get_dpr()
        return self.world_to_screen(world_vec, view_width, view_height, dpr)
    
    def convert_image_to_viewport(self, x: float, y: float) -> QPointF:
        """Convert image pixel coordinates to viewport coordinates."""
        texture_size = self._texture_size_provider()
        scale = self.get_effective_scale()
        view_width, view_height = self._get_view_dimensions_logical()
        dpr = self._get_dpr()
        return self.image_to_viewport(x, y, texture_size, scale, view_width, view_height, dpr)
    
    def convert_viewport_to_image(self, point: QPointF) -> QPointF:
        """Convert viewport coordinates to image pixel coordinates."""
        texture_size = self._texture_size_provider()
        scale = self.get_effective_scale()
        view_width, view_height = self._get_view_dimensions_logical()
        dpr = self._get_dpr()
        return self.viewport_to_image(point, texture_size, scale, view_width, view_height, dpr)
