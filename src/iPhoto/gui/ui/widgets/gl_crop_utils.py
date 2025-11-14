"""
Crop-related data structures and utility functions for the GL image viewer.

This module contains pure functions and data structures that support the crop
functionality without any direct dependency on QOpenGLWidget or Qt event handling.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping

from PySide6.QtCore import QPointF, Qt


class CropHandle(enum.IntEnum):
    """Enumeration of crop box interaction handles."""

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
    """Return the appropriate cursor shape for a given crop handle."""
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
    """Cubic easing function for smooth animations (ease-out)."""
    return 1.0 - (1.0 - t) ** 3


def ease_in_quad(t: float) -> float:
    """Quadratic easing function for smooth animations (ease-in)."""
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
        """Initialize crop state from a mapping of adjustment values."""
        self.cx = float(values.get("Crop_CX", 0.5))
        self.cy = float(values.get("Crop_CY", 0.5))
        self.width = float(values.get("Crop_W", 1.0))
        self.height = float(values.get("Crop_H", 1.0))
        self.clamp()

    def as_mapping(self) -> dict[str, float]:
        """Export crop state as a mapping of adjustment values."""
        return {
            "Crop_CX": float(self.cx),
            "Crop_CY": float(self.cy),
            "Crop_W": float(self.width),
            "Crop_H": float(self.height),
        }

    def set_full(self) -> None:
        """Reset crop to full image."""
        self.cx = 0.5
        self.cy = 0.5
        self.width = 1.0
        self.height = 1.0

    def bounds_normalised(self) -> tuple[float, float, float, float]:
        """Return (left, top, right, bottom) in normalised coordinates [0,1]."""
        half_w = self.width * 0.5
        half_h = self.height * 0.5
        return (
            self.cx - half_w,
            self.cy - half_h,
            self.cx + half_w,
            self.cy + half_h,
        )

    def to_pixel_rect(self, image_width: int, image_height: int) -> dict[str, float]:
        """Convert normalised crop to pixel coordinates."""
        left_n, top_n, right_n, bottom_n = self.bounds_normalised()
        return {
            "left": left_n * image_width,
            "top": top_n * image_height,
            "right": right_n * image_width,
            "bottom": bottom_n * image_height,
        }

    def center_pixels(self, image_width: int, image_height: int) -> QPointF:
        """Return crop center in pixel coordinates."""
        return QPointF(self.cx * image_width, self.cy * image_height)

    def translate_pixels(self, delta: QPointF, image_size: tuple[int, int]) -> None:
        """Move crop box by delta in pixel coordinates."""
        iw, ih = image_size
        if iw <= 0 or ih <= 0:
            return
        self.cx += float(delta.x()) / float(iw)
        self.cy += float(delta.y()) / float(ih)
        self.clamp()

    def drag_edge_pixels(
        self, handle: CropHandle, delta: QPointF, image_size: tuple[int, int]
    ) -> None:
        """Resize crop box by dragging an edge or corner."""
        iw, ih = image_size
        if iw <= 0 or ih <= 0:
            return
        dx = float(delta.x()) / float(iw)
        dy = float(delta.y()) / float(ih)
        left, top, right, bottom = self.bounds_normalised()
        min_w = max(self.min_width, 1.0 / max(1.0, float(iw)))
        min_h = max(self.min_height, 1.0 / max(1.0, float(ih)))

        # Update edges incrementally to avoid order-dependency issues
        # Each edge update keeps the opposite edge fixed and recalculates center and size
        if handle in (CropHandle.LEFT, CropHandle.TOP_LEFT, CropHandle.BOTTOM_LEFT):
            new_left = left + dx
            new_left = min(new_left, right - min_w)
            new_left = max(new_left, 0.0)
            # Keep right fixed, recalculate width and cx
            self.width = right - new_left
            self.cx = new_left + self.width * 0.5
            left = new_left  # Update left for potential subsequent edge updates

        if handle in (CropHandle.RIGHT, CropHandle.TOP_RIGHT, CropHandle.BOTTOM_RIGHT):
            new_right = right + dx
            new_right = max(new_right, left + min_w)
            new_right = min(new_right, 1.0)
            # Keep left fixed, recalculate width and cx
            self.width = new_right - left
            self.cx = left + self.width * 0.5
            right = new_right  # Update right for potential subsequent edge updates

        if handle in (CropHandle.BOTTOM, CropHandle.BOTTOM_LEFT, CropHandle.BOTTOM_RIGHT):
            new_bottom = bottom + dy
            new_bottom = max(new_bottom, top + min_h)
            new_bottom = min(new_bottom, 1.0)
            # Keep top fixed, recalculate height and cy
            self.height = new_bottom - top
            self.cy = top + self.height * 0.5
            bottom = new_bottom  # Update bottom for potential subsequent edge updates

        if handle in (CropHandle.TOP, CropHandle.TOP_LEFT, CropHandle.TOP_RIGHT):
            new_top = top + dy
            new_top = min(new_top, bottom - min_h)
            new_top = max(new_top, 0.0)
            # Keep bottom fixed, recalculate height and cy
            self.height = bottom - new_top
            self.cy = new_top + self.height * 0.5

        self.clamp()

    def clamp(self) -> None:
        """Ensure crop state remains within valid bounds."""
        self.width = max(self.min_width, min(1.0, self.width))
        self.height = max(self.min_height, min(1.0, self.height))
        half_w = self.width * 0.5
        half_h = self.height * 0.5
        self.cx = max(half_w, min(1.0 - half_w, self.cx))
        self.cy = max(half_h, min(1.0 - half_h, self.cy))
