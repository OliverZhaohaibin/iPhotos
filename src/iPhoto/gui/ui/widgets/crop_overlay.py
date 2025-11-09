"""Interactive overlay used when the edit viewer enters crop mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


@dataclass
class _DragState:
    mode: Optional[str] = None
    start_pos: QPointF = QPointF()
    start_rect: QRectF = QRectF()


class CropOverlay(QWidget):
    """Translucent overlay that exposes resize handles for cropping."""

    crop_finished = Signal(QRectF)
    """Emitted with the final selection rectangle in normalised widget space."""

    HANDLE_SIZE = 9.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)
        self._rect01 = QRectF(0.0, 0.0, 1.0, 1.0)
        self._bounds01 = QRectF(0.0, 0.0, 1.0, 1.0)
        self._drag_state = _DragState()
        self._minimum_size_px = QSize(20, 20)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_selection_rect(self, rect01: QRectF) -> None:
        """Update the currently selected rectangle (normalised coordinates)."""

        self._rect01 = QRectF(rect01)
        self.update()

    def set_bounds_rect(self, rect01: QRectF) -> None:
        """Clamp the selectable area to *rect01* (normalised coordinates)."""

        self._bounds01 = QRectF(rect01)
        self._rect01 = self._rect01.intersected(self._bounds01)
        if self._rect01.isEmpty():
            self._rect01 = QRectF(self._bounds01)
        self.update()

    def selection_rect(self) -> QRectF:
        return QRectF(self._rect01)

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    def _rect_to_px(self, rect01: QRectF) -> QRectF:
        width = max(1.0, float(self.width()))
        height = max(1.0, float(self.height()))
        return QRectF(
            rect01.x() * width,
            rect01.y() * height,
            rect01.width() * width,
            rect01.height() * height,
        )

    def _rect_from_px(self, rect_px: QRectF) -> QRectF:
        width = max(1.0, float(self.width()))
        height = max(1.0, float(self.height()))
        rect_px = rect_px.normalized()
        return QRectF(
            rect_px.x() / width,
            rect_px.y() / height,
            rect_px.width() / width,
            rect_px.height() / height,
        )

    def _bounds_rect_px(self) -> QRectF:
        return self._rect_to_px(self._bounds01)

    def _selection_rect_px(self) -> QRectF:
        return self._rect_to_px(self._rect01)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        mode = self._hit_test(event.position())
        if mode is None:
            return
        self._drag_state = _DragState(
            mode=mode,
            start_pos=event.position(),
            start_rect=self._selection_rect_px(),
        )
        self.grabMouse()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._drag_state.mode is None:
            self.update()
            return
        delta = event.position() - self._drag_state.start_pos
        rect = QRectF(self._drag_state.start_rect)
        mode = self._drag_state.mode
        if mode == "move":
            rect.translate(delta)
        elif mode == "l":
            rect.setLeft(rect.left() + delta.x())
        elif mode == "r":
            rect.setRight(rect.right() + delta.x())
        elif mode == "t":
            rect.setTop(rect.top() + delta.y())
        elif mode == "b":
            rect.setBottom(rect.bottom() + delta.y())
        elif mode == "tl":
            rect.setLeft(rect.left() + delta.x())
            rect.setTop(rect.top() + delta.y())
        elif mode == "tr":
            rect.setRight(rect.right() + delta.x())
            rect.setTop(rect.top() + delta.y())
        elif mode == "bl":
            rect.setLeft(rect.left() + delta.x())
            rect.setBottom(rect.bottom() + delta.y())
        elif mode == "br":
            rect.setRight(rect.right() + delta.x())
            rect.setBottom(rect.bottom() + delta.y())

        rect = rect.normalized()
        rect = self._clamp_to_bounds(rect)
        self._rect01 = self._rect_from_px(rect)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self._drag_state.mode is not None:
            self.releaseMouse()
            rect = self.selection_rect()
            self._drag_state = _DragState()
            self.crop_finished.emit(rect)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        selection = self._selection_rect_px()
        painter.fillRect(self.rect(), QColor(0, 0, 0, 96))
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(selection, QColor(0, 0, 0, 0))
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        pen = QPen(QColor(255, 255, 255, 220), 2.0)
        painter.setPen(pen)
        painter.drawRect(selection.adjusted(0.5, 0.5, -0.5, -0.5))

        guide_pen = QPen(QColor(255, 255, 255, 150), 1.0, Qt.PenStyle.DashLine)
        painter.setPen(guide_pen)
        one_third_w = selection.width() / 3.0
        one_third_h = selection.height() / 3.0
        painter.drawLine(
            selection.left() + one_third_w,
            selection.top(),
            selection.left() + one_third_w,
            selection.bottom(),
        )
        painter.drawLine(
            selection.left() + 2 * one_third_w,
            selection.top(),
            selection.left() + 2 * one_third_w,
            selection.bottom(),
        )
        painter.drawLine(
            selection.left(),
            selection.top() + one_third_h,
            selection.right(),
            selection.top() + one_third_h,
        )
        painter.drawLine(
            selection.left(),
            selection.top() + 2 * one_third_h,
            selection.right(),
            selection.top() + 2 * one_third_h,
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255))
        handle = self.HANDLE_SIZE
        half = handle / 2.0
        for point in (
            selection.topLeft(),
            selection.topRight(),
            selection.bottomLeft(),
            selection.bottomRight(),
        ):
            painter.drawRect(QRectF(point - QPointF(half, half), QSize(handle, handle)))

        painter.end()

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------
    def _hit_test(self, pos: QPointF) -> Optional[str]:
        rect = self._selection_rect_px()
        handle = self.HANDLE_SIZE
        corners = {
            "tl": QRectF(
                rect.topLeft() - QPointF(handle, handle),
                QSize(handle * 2.0, handle * 2.0),
            ),
            "tr": QRectF(
                rect.topRight() - QPointF(handle, handle),
                QSize(handle * 2.0, handle * 2.0),
            ),
            "bl": QRectF(
                rect.bottomLeft() - QPointF(handle, handle),
                QSize(handle * 2.0, handle * 2.0),
            ),
            "br": QRectF(
                rect.bottomRight() - QPointF(handle, handle),
                QSize(handle * 2.0, handle * 2.0),
            ),
        }
        for key, area in corners.items():
            if area.contains(pos):
                return key
        if QRectF(
            rect.left() - handle,
            rect.top() + handle,
            handle * 2.0,
            rect.height() - handle * 2.0,
        ).contains(pos):
            return "l"
        if QRectF(
            rect.right() - handle,
            rect.top() + handle,
            handle * 2.0,
            rect.height() - handle * 2.0,
        ).contains(pos):
            return "r"
        if QRectF(
            rect.left() + handle,
            rect.top() - handle,
            rect.width() - handle * 2.0,
            handle * 2.0,
        ).contains(pos):
            return "t"
        if QRectF(
            rect.left() + handle,
            rect.bottom() - handle,
            rect.width() - handle * 2.0,
            handle * 2.0,
        ).contains(pos):
            return "b"
        if rect.contains(pos):
            return "move"
        return None

    def _clamp_to_bounds(self, rect: QRectF) -> QRectF:
        bounds = self._bounds_rect_px()
        min_w = float(self._minimum_size_px.width())
        min_h = float(self._minimum_size_px.height())

        rect.setLeft(max(bounds.left(), min(rect.left(), bounds.right() - min_w)))
        rect.setTop(max(bounds.top(), min(rect.top(), bounds.bottom() - min_h)))
        rect.setRight(max(rect.left() + min_w, min(rect.right(), bounds.right())))
        rect.setBottom(max(rect.top() + min_h, min(rect.bottom(), bounds.bottom())))
        return rect

