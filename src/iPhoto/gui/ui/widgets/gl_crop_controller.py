"""
Crop interaction controller for the GL image viewer.

This module encapsulates all crop-related interaction logic, state management,
and animation handling, keeping the main viewer focused on coordination.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping

from PySide6.QtCore import QPointF, Qt, QTimer, QObject
from PySide6.QtGui import QMouseEvent, QWheelEvent

from .gl_crop_utils import CropBoxState, CropHandle, cursor_for_handle, ease_in_quad, ease_out_cubic
from .view_transform_controller import compute_fit_to_view_scale

_LOGGER = logging.getLogger(__name__)


class CropInteractionController:
    """Manages all crop mode interactions, animations, and state."""

    def __init__(
        self,
        *,
        texture_size_provider: Callable[[], tuple[int, int]],
        clamp_image_center_to_crop: Callable[[QPointF, float], QPointF],
        transform_controller,  # ViewTransformController
        on_crop_changed: Callable[[float, float, float, float], None],
        on_cursor_change: Callable[[Qt.CursorShape | None], None],
        on_request_update: Callable[[], None],
        timer_parent: QObject | None = None,
    ) -> None:
        """Initialize the crop interaction controller.

        Parameters
        ----------
        texture_size_provider:
            Callable that returns (width, height) of the current texture.
        clamp_image_center_to_crop:
            Callable to clamp image center to crop bounds.
        transform_controller:
            ViewTransformController instance for zoom/pan and coordinate transforms.
        on_crop_changed:
            Callback when crop values change, signature: (cx, cy, width, height).
        on_cursor_change:
            Callback to change cursor, signature: (cursor_shape or None to unset).
        on_request_update:
            Callback to request widget update/repaint.
        timer_parent:
            Parent QObject for timers (optional).
        """
        self._texture_size_provider = texture_size_provider
        self._clamp_image_center_to_crop = clamp_image_center_to_crop
        self._transform_controller = transform_controller
        self._on_crop_changed = on_crop_changed
        self._on_cursor_change = on_cursor_change
        self._on_request_update = on_request_update

        # Crop state
        self._active: bool = False
        self._crop_state = CropBoxState()
        self._crop_drag_handle: CropHandle = CropHandle.NONE
        self._crop_dragging: bool = False
        self._crop_last_pos = QPointF()
        self._crop_hit_padding: float = 12.0
        self._crop_edge_threshold: float = 48.0
        self._crop_drag_anchor_viewport: tuple[QPointF, QPointF] | None = None

        # Crop model transform (independent from camera)
        self._crop_img_offset = QPointF(0.0, 0.0)
        self._crop_img_scale: float = 1.0
        self._img_scale_clamp: tuple[float, float] = (0.02, 40.0)

        # Animation state
        self._crop_idle_timer = QTimer(timer_parent)
        self._crop_idle_timer.setInterval(1000)
        self._crop_idle_timer.timeout.connect(self._on_crop_idle_timeout)
        self._crop_anim_timer = QTimer(timer_parent)
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_active(self) -> bool:
        """Return True if crop mode is currently active."""
        return self._active

    def get_crop_values(self) -> dict[str, float]:
        """Return the current crop state as a mapping."""
        return self._crop_state.as_mapping()

    def get_crop_state(self) -> CropBoxState:
        """Return the current crop state object."""
        return self._crop_state

    def get_crop_model_transform(self) -> tuple[QPointF, float]:
        """Return (offset, scale) for the crop-specific model transform."""
        return (QPointF(self._crop_img_offset), self._crop_img_scale)

    def is_faded_out(self) -> bool:
        """Return True if the crop overlay is currently faded out."""
        return self._crop_faded_out

    def current_crop_rect_pixels(self) -> dict[str, float] | None:
        """Return the crop rectangle in viewport device pixels."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return None
        rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        top_left = self._transform_controller.convert_image_to_viewport(rect["left"], rect["top"])
        bottom_right = self._transform_controller.convert_image_to_viewport(rect["right"], rect["bottom"])
        dpr = self._transform_controller._get_dpr()
        return {
            "left": top_left.x() * dpr,
            "top": top_left.y() * dpr,
            "right": bottom_right.x() * dpr,
            "bottom": bottom_right.y() * dpr,
        }

    def set_active(self, enabled: bool, values: Mapping[str, float] | None = None) -> None:
        """Enable or disable crop mode with optional initial crop values."""
        if enabled == self._active:
            if enabled and values is not None:
                self._apply_crop_values(values)
            return

        self._active = bool(enabled)
        if not self._active:
            self._stop_crop_animation()
            self._crop_idle_timer.stop()
            self._crop_drag_handle = CropHandle.NONE
            self._crop_dragging = False
            self._crop_faded_out = False
            self._reset_crop_model_transform()
            self._on_cursor_change(None)
            self._on_request_update()
            return

        # Reset the image model transform when entering crop mode
        self._reset_crop_model_transform()
        self._apply_crop_values(values)
        self._crop_faded_out = False
        self._crop_drag_handle = CropHandle.NONE
        self._crop_dragging = False
        self._stop_crop_animation()
        self._restart_crop_idle()
        self._on_request_update()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def handle_mouse_press(self, event: QMouseEvent) -> None:
        """Handle mouse press events in crop mode."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return

        self._stop_crop_animation()
        self._stop_crop_idle()
        self._crop_faded_out = False
        pos = event.position()
        handle = self._crop_hit_test(pos)

        if handle == CropHandle.NONE:
            self._crop_drag_handle = CropHandle.NONE
            self._crop_dragging = False
            self._on_cursor_change(Qt.CursorShape.ArrowCursor)
            return

        self._crop_drag_handle = handle
        self._crop_dragging = True
        self._crop_last_pos = QPointF(pos)

        if handle == CropHandle.INSIDE:
            # Cache viewport anchor points for inside dragging
            rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
            top_left = self._transform_controller.convert_image_to_viewport(rect["left"], rect["top"])
            bottom_right = self._transform_controller.convert_image_to_viewport(rect["right"], rect["bottom"])
            self._crop_drag_anchor_viewport = (QPointF(top_left), QPointF(bottom_right))
            self._on_cursor_change(Qt.CursorShape.ClosedHandCursor)
        else:
            self._crop_drag_anchor_viewport = None
            self._on_cursor_change(cursor_for_handle(handle))

        event.accept()

    def handle_mouse_move(self, event: QMouseEvent) -> None:
        """Handle mouse move events in crop mode."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return

        pos = event.position()
        if not self._crop_dragging:
            handle = self._crop_hit_test(pos)
            self._on_cursor_change(cursor_for_handle(handle))
            return

        previous_pos = QPointF(self._crop_last_pos)
        delta_view = pos - previous_pos
        self._crop_last_pos = QPointF(pos)
        self._crop_faded_out = False

        if self._crop_drag_handle == CropHandle.INSIDE:
            # Dragging inside: move the view, not the crop
            view_scale = self._transform_controller.get_effective_scale()
            if view_scale <= 1e-6:
                return

            dpr = self._transform_controller._get_dpr()
            delta_device_x = float(delta_view.x()) * dpr
            delta_device_y = float(delta_view.y()) * dpr

            delta_world = QPointF(
                delta_device_x / view_scale,
                -delta_device_y / view_scale,
            )

            tentative_offset = QPointF(
                self._crop_img_offset.x() + delta_world.x(),
                self._crop_img_offset.y() + delta_world.y(),
            )

            clamped_offset = self._clamp_crop_img_offset(tentative_offset, self._crop_img_scale)
            self._crop_img_offset = clamped_offset
        else:
            # Dragging edge/corner: resize the crop
            scale = self._transform_controller.get_effective_scale() * self._crop_img_scale
            if scale <= 1e-6:
                return
            dpr = self._transform_controller._get_dpr()
            image_delta = QPointF(
                delta_view.x() * dpr / scale,
                delta_view.y() * dpr / scale,
            )
            tex_size = self._texture_size_provider()
            self._crop_state.drag_edge_pixels(self._crop_drag_handle, image_delta, tex_size)
            self._auto_shrink_on_drag(delta_view)
            self._emit_crop_changed()

        self._restart_crop_idle()
        self._on_request_update()

    def handle_mouse_release(self, event: QMouseEvent) -> None:
        """Handle mouse release events in crop mode."""
        del event  # unused
        self._crop_dragging = False
        self._crop_drag_handle = CropHandle.NONE
        self._on_cursor_change(None)
        self._crop_drag_anchor_viewport = None
        self._restart_crop_idle()

    def handle_wheel(self, event: QWheelEvent) -> None:
        """Handle wheel events in crop mode for zooming."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return

        self._stop_crop_animation()
        self._crop_faded_out = False
        self._stop_crop_idle()

        angle = event.angleDelta().y()
        if angle == 0:
            self._restart_crop_idle()
            return

        # Guard against devices that emit unusually large wheel deltas
        angle = max(-480, min(480, angle))

        factor = math.pow(1.0015, angle)
        dynamic_min = self._dynamic_min_scale_for_crop()
        min_allowed = max(self._img_scale_clamp[0], dynamic_min)
        max_allowed = self._img_scale_clamp[1]

        new_scale_raw = self._crop_img_scale * factor
        new_scale = max(min_allowed, min(max_allowed, new_scale_raw))

        if abs(new_scale - self._crop_img_scale) <= 1e-6:
            self._restart_crop_idle()
            event.accept()
            return

        view_scale = self._transform_controller.get_effective_scale()
        if view_scale <= 1e-6:
            self._restart_crop_idle()
            event.accept()
            return

        world_point = self._transform_controller.convert_screen_to_world(event.position())
        view_pan = self._transform_controller.get_pan_pixels()
        screen_vector = QPointF(
            world_point.x() - view_pan.x(),
            world_point.y() - view_pan.y(),
        )

        anchor_world = QPointF(
            screen_vector.x() / view_scale,
            screen_vector.y() / view_scale,
        )

        scale_ratio = new_scale / max(self._crop_img_scale, 1e-6)
        new_offset = QPointF(
            anchor_world.x() + (self._crop_img_offset.x() - anchor_world.x()) * scale_ratio,
            anchor_world.y() + (self._crop_img_offset.y() - anchor_world.y()) * scale_ratio,
        )

        new_offset = self._clamp_crop_img_offset(new_offset, new_scale)

        self._crop_img_scale = new_scale
        self._crop_img_offset = new_offset
        self._on_request_update()
        self._restart_crop_idle()
        event.accept()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _reset_crop_model_transform(self) -> None:
        """Return the crop-specific model transform to its neutral state."""
        self._crop_img_offset = QPointF(0.0, 0.0)
        self._crop_img_scale = 1.0

    def _apply_crop_values(self, values: Mapping[str, float] | None) -> None:
        """Apply crop values to the crop state."""
        if values:
            self._crop_state.set_from_mapping(values)
        else:
            self._crop_state.set_full()

        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return

        center = self._crop_state.center_pixels(tex_w, tex_h)
        scale = self._transform_controller.get_effective_scale()
        clamped_center = self._clamp_image_center_to_crop(center, scale)
        self._transform_controller.apply_image_center_pixels(clamped_center, scale)

    def _clamp_crop_img_offset(self, offset: QPointF, scale: float) -> QPointF:
        """Clamp the model transform so the crop never exposes empty pixels."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return offset
        if scale <= 1e-9:
            return offset

        crop_rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        crop_left = float(crop_rect["left"])
        crop_top = float(crop_rect["top"])
        crop_right = float(crop_rect["right"])
        crop_bottom = float(crop_rect["bottom"])

        crop_center_x = (crop_left + crop_right) * 0.5
        crop_center_y = (crop_top + crop_bottom) * 0.5
        crop_width = max(1.0, crop_right - crop_left)
        crop_height = max(1.0, crop_bottom - crop_top)

        # Convert to world-space coordinate system
        crop_center_world_x = crop_center_x - (tex_w * 0.5)
        crop_center_world_y = (tex_h * 0.5) - crop_center_y
        half_crop_w = crop_width * 0.5
        half_crop_h = crop_height * 0.5

        crop_left_world = crop_center_world_x - half_crop_w
        crop_right_world = crop_center_world_x + half_crop_w
        crop_bottom_world = crop_center_world_y - half_crop_h
        crop_top_world = crop_center_world_y + half_crop_h

        half_image_w = (tex_w * scale) * 0.5
        half_image_h = (tex_h * scale) * 0.5

        min_offset_x = crop_right_world - half_image_w
        max_offset_x = crop_left_world + half_image_w
        min_offset_y = crop_top_world - half_image_h
        max_offset_y = crop_bottom_world + half_image_h

        clamped_x = max(min_offset_x, min(max_offset_x, float(offset.x())))
        clamped_y = max(min_offset_y, min(max_offset_y, float(offset.y())))
        return QPointF(clamped_x, clamped_y)

    def _dynamic_min_scale_for_crop(self) -> float:
        """Return the minimum model scale that keeps the crop fully covered."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return 0.0

        crop_rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        crop_width = max(1.0, float(crop_rect["right"] - crop_rect["left"]))
        crop_height = max(1.0, float(crop_rect["bottom"] - crop_rect["top"]))

        width_ratio = crop_width / max(1.0, float(tex_w))
        height_ratio = crop_height / max(1.0, float(tex_h))
        return max(width_ratio, height_ratio)

    def _crop_center_viewport_point(self) -> QPointF:
        """Return the crop center in viewport coordinates."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            # Fallback to viewport center
            view_width, view_height = self._transform_controller._get_view_dimensions_logical()
            return QPointF(view_width / 2, view_height / 2)
        center = self._crop_state.center_pixels(tex_w, tex_h)
        return self._transform_controller.convert_image_to_viewport(center.x(), center.y())

    @staticmethod
    def _distance_to_segment(point: QPointF, start: QPointF, end: QPointF) -> float:
        """Calculate distance from point to line segment."""
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
        """Determine which crop handle (if any) is under the cursor."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return CropHandle.NONE

        rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        top_left = self._transform_controller.convert_image_to_viewport(rect["left"], rect["top"])
        top_right = self._transform_controller.convert_image_to_viewport(rect["right"], rect["top"])
        bottom_right = self._transform_controller.convert_image_to_viewport(rect["right"], rect["bottom"])
        bottom_left = self._transform_controller.convert_image_to_viewport(rect["left"], rect["bottom"])

        # Check corners first
        corners = [
            (CropHandle.TOP_LEFT, top_left),
            (CropHandle.TOP_RIGHT, top_right),
            (CropHandle.BOTTOM_RIGHT, bottom_right),
            (CropHandle.BOTTOM_LEFT, bottom_left),
        ]
        for handle, corner in corners:
            if math.hypot(point.x() - corner.x(), point.y() - corner.y()) <= self._crop_hit_padding:
                return handle

        # Check edges
        edges = [
            (CropHandle.TOP, top_left, top_right),
            (CropHandle.RIGHT, top_right, bottom_right),
            (CropHandle.BOTTOM, bottom_left, bottom_right),
            (CropHandle.LEFT, top_left, bottom_left),
        ]
        for handle, start, end in edges:
            if self._distance_to_segment(point, start, end) <= self._crop_hit_padding:
                return handle

        # Check if inside
        left = min(top_left.x(), bottom_left.x())
        right = max(top_right.x(), bottom_right.x())
        top = min(top_left.y(), top_right.y())
        bottom = max(bottom_left.y(), bottom_right.y())
        if left <= point.x() <= right and top <= point.y() <= bottom:
            return CropHandle.INSIDE

        return CropHandle.NONE

    def _restart_crop_idle(self) -> None:
        """Restart the idle timer for crop fade-out animation."""
        if self._active:
            self._crop_idle_timer.start()

    def _stop_crop_idle(self) -> None:
        """Stop the idle timer."""
        self._crop_idle_timer.stop()

    def _stop_crop_animation(self) -> None:
        """Stop the crop fade-out animation."""
        if self._crop_anim_active:
            self._crop_anim_active = False
            self._crop_anim_timer.stop()

    def _on_crop_idle_timeout(self) -> None:
        """Handle idle timeout - start fade-out animation."""
        self._crop_idle_timer.stop()
        self._start_crop_animation()

    def _start_crop_animation(self) -> None:
        """Start the crop fade-out animation."""
        tex_w, tex_h = self._texture_size_provider()
        if not self._active or tex_w <= 0 or tex_h <= 0:
            return

        target_scale = self._target_scale_for_crop()
        target_center = self._crop_state.center_pixels(tex_w, tex_h)
        target_center = self._clamp_image_center_to_crop(target_center, target_scale)

        self._crop_anim_active = True
        self._crop_anim_start_time = time.monotonic()
        self._crop_anim_start_scale = self._transform_controller.get_effective_scale()
        self._crop_anim_target_scale = target_scale
        self._crop_anim_start_center = self._transform_controller.get_image_center_pixels()
        self._crop_anim_target_center = target_center
        self._crop_anim_timer.start()
        self._crop_faded_out = False

    def _on_crop_anim_tick(self) -> None:
        """Handle animation tick - update crop animation state."""
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
            self._on_request_update()
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
        self._on_request_update()

    def _apply_crop_animation_state(self, scale: float, centre: QPointF) -> None:
        """Apply animation state to the viewer."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return

        vw, vh = self._transform_controller._get_view_dimensions_device_px()
        tex_size = (tex_w, tex_h)
        base_scale = compute_fit_to_view_scale(tex_size, vw, vh)
        min_zoom = self._transform_controller.minimum_zoom()
        max_zoom = self._transform_controller.maximum_zoom()
        zoom_factor = max(min_zoom, min(max_zoom, scale / max(base_scale, 1e-6)))
        self._transform_controller.set_zoom_factor_direct(zoom_factor)
        actual_scale = self._transform_controller.get_effective_scale()
        clamped_center = self._clamp_image_center_to_crop(centre, actual_scale)
        self._transform_controller.apply_image_center_pixels(clamped_center, actual_scale)

    def _target_scale_for_crop(self) -> float:
        """Calculate the target scale for crop fade-out animation."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return self._transform_controller.get_effective_scale()

        vw, vh = self._transform_controller._get_view_dimensions_device_px()
        crop_rect = self._crop_state.to_pixel_rect(tex_w, tex_h)
        crop_width = max(1.0, crop_rect["right"] - crop_rect["left"])
        crop_height = max(1.0, crop_rect["bottom"] - crop_rect["top"])
        padding = 20.0 * self._transform_controller._get_dpr()
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
        """Auto zoom-out while pushing edges against viewport boundaries."""
        tex_w, tex_h = self._texture_size_provider()
        if tex_w <= 0 or tex_h <= 0:
            return

        vw, vh = self._transform_controller._get_view_dimensions_device_px()
        crop_rect = self.current_crop_rect_pixels()
        if crop_rect is None:
            return

        threshold = self._crop_edge_threshold
        delta_x = delta.x()
        delta_y = delta.y()

        # Convert delta to image space
        dpr = self._transform_controller._get_dpr()
        current_scale = self._transform_controller.get_effective_scale()
        if current_scale <= 1e-6:
            return

        image_delta = QPointF(delta_x * dpr / current_scale, delta_y * dpr / current_scale)
        world_delta_x = image_delta.x()
        world_delta_y = -image_delta.y()  # Flip Y: texture down = world up

        # Calculate pressure and d_offset
        pressure = 0.0
        d_offset_x = 0.0
        d_offset_y = 0.0

        # Left edge pushing out
        if (
            self._crop_drag_handle in (CropHandle.LEFT, CropHandle.TOP_LEFT, CropHandle.BOTTOM_LEFT)
            and delta_x < 0.0
        ):
            left_margin = crop_rect["left"]
            if left_margin < threshold:
                p = (threshold - left_margin) / threshold
                pressure = max(pressure, p)
                d_offset_x = max(d_offset_x, world_delta_x * -p)

        # Right edge pushing out
        if (
            self._crop_drag_handle
            in (CropHandle.RIGHT, CropHandle.TOP_RIGHT, CropHandle.BOTTOM_RIGHT)
            and delta_x > 0.0
        ):
            right_margin = vw - crop_rect["right"]
            if right_margin < threshold:
                p = (threshold - right_margin) / threshold
                pressure = max(pressure, p)
                d_offset_x = min(d_offset_x, world_delta_x * -p)

        # Top edge pushing out
        if (
            self._crop_drag_handle in (CropHandle.TOP, CropHandle.TOP_LEFT, CropHandle.TOP_RIGHT)
            and delta_y < 0.0
        ):
            top_margin = crop_rect["top"]
            if top_margin < threshold:
                p = (threshold - top_margin) / threshold
                pressure = max(pressure, p)
                d_offset_y = min(d_offset_y, world_delta_y * -p)

        # Bottom edge pushing out
        if (
            self._crop_drag_handle
            in (CropHandle.BOTTOM, CropHandle.BOTTOM_LEFT, CropHandle.BOTTOM_RIGHT)
            and delta_y > 0.0
        ):
            bottom_margin = vh - crop_rect["bottom"]
            if bottom_margin < threshold:
                p = (threshold - bottom_margin) / threshold
                pressure = max(pressure, p)
                d_offset_y = max(d_offset_y, world_delta_y * -p)

        if pressure <= 0.0:
            return

        # Ease the pressure for smooth feel
        eased_pressure = ease_in_quad(min(1.0, pressure))

        # Scale down around crop center
        tex_size = (tex_w, tex_h)
        vw_float, vh_float = float(vw), float(vh)
        base_scale = compute_fit_to_view_scale(tex_size, vw_float, vh_float)

        min_scale = max(base_scale, base_scale * self._transform_controller.minimum_zoom())
        max_scale = base_scale * self._transform_controller.maximum_zoom()

        k_max = 0.05  # Maximum shrink ratio per event
        factor = 1.0 - k_max * eased_pressure
        new_scale = max(min_scale, min(max_scale, current_scale * factor))

        # Scale around crop center
        anchor = self._crop_center_viewport_point()
        self._transform_controller.set_zoom(new_scale / max(base_scale, 1e-6), anchor=anchor)

        # Apply pan_gain and translate the view
        pan_gain = 0.75 + 0.25 * eased_pressure
        final_d_offset = QPointF(d_offset_x * pan_gain, -d_offset_y * pan_gain)

        if abs(final_d_offset.x()) > 1e-4 or abs(final_d_offset.y()) > 1e-4:
            new_center = self._transform_controller.get_image_center_pixels() + final_d_offset
            actual_scale = self._transform_controller.get_effective_scale()
            clamped = self._clamp_image_center_to_crop(new_center, actual_scale)
            self._transform_controller.apply_image_center_pixels(clamped, actual_scale)

    def _emit_crop_changed(self) -> None:
        """Emit the crop changed signal."""
        state = self._crop_state
        self._on_crop_changed(
            float(state.cx), float(state.cy), float(state.width), float(state.height)
        )
