# -*- coding: utf-8 -*-
"""Utilities for handling zoom and pan interaction in the GL image viewer."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, Qt
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
        on_next_item: Optional[Callable[[], None]] = None,
        on_prev_item: Optional[Callable[[], None]] = None,
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
    # State accessors
    # ------------------------------------------------------------------
    def get_zoom_factor(self) -> float:
        return self._zoom_factor

    def get_pan_pixels(self) -> QPointF:
        return QPointF(self._pan_px)

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
    def set_zoom(self, factor: float, anchor: Optional[QPointF] = None) -> bool:
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
                anchor_bottom_left = QPointF(anchor_point.x() * dpr, view_height - anchor_point.y() * dpr)
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

        changed = abs(self._zoom_factor - 1.0) > 1e-6 or abs(self._pan_px.x()) > 1e-6 or abs(self._pan_px.y()) > 1e-6
        self._zoom_factor = 1.0
        self._pan_px = QPointF(0.0, 0.0)
        self._viewer.update()
        self._on_zoom_changed(self._zoom_factor)
        return changed

    def set_pan_pixels(self, pan: QPointF) -> None:
        """Directly set pan in view pixels (origin at viewport center)."""
        self._pan_px = QPointF(float(pan.x()), float(pan.y()))
        self._viewer.update()

    def center_view(self) -> None:
        """Center content in the viewport by zeroing the pan."""
        self.set_pan_pixels(QPointF(0.0, 0.0))

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
