"""Custom segmented control used by the edit header."""

from __future__ import annotations

from math import floor
from typing import Iterable, List, Sequence

from PySide6.QtCore import (
    QEasingCurve,
    Property,
    QPointF,
    QRectF,
    Qt,
    QPropertyAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QSizePolicy, QVBoxLayout, QWidget


def _snap05(value: float) -> float:
    """Align *value* to the nearest half pixel to keep strokes crisp."""

    return floor(value) + 0.5


def _align_rect_05(rect: QRectF) -> QRectF:
    """Return *rect* aligned to the half-pixel grid."""

    x = _snap05(rect.x())
    y = _snap05(rect.y())
    width = round(rect.width())
    height = round(rect.height())
    return QRectF(x, y, width, height)


class SegmentedTopBar(QWidget):
    """Rounded segmented control styled to mirror the Photos.app toolbar."""

    currentIndexChanged = Signal(int)

    def __init__(self, items: Sequence[str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        provided_items: List[str] = list(items) if items is not None else ["Adjust", "Filters", "Crop"]
        self._items: List[str] = provided_items
        self._index = 0
        self._anim_pos = float(self._index)
        self._anim = QPropertyAnimation(self, b"animPos", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Visual parameters -------------------------------------------------
        self.h_pad = 10
        self.v_pad = 6
        self.radius = 12
        self.sep_inset = 6
        self.sep_width = 1.0
        self.height_hint = 36

        self.bg = QColor(48, 48, 48)
        self.border = QColor(70, 70, 70)
        self.text_active = QColor(250, 250, 250)
        self.text_inactive = QColor(180, 180, 180)
        self.frosty_a = QColor(255, 255, 255, 42)
        self.frosty_b = QColor(255, 255, 255, 18)

        self.setMinimumHeight(self.height_hint)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------
    # Public API
    def items(self) -> List[str]:
        """Return a copy of the current segment labels."""

        return list(self._items)

    def setItems(self, items: Iterable[str]) -> None:
        """Replace the displayed segments with *items*."""

        new_items = list(items) or ["Item"]
        self._items = new_items
        self._index = max(0, min(self._index, len(self._items) - 1))
        self._anim_pos = float(self._index)
        self.update()
        self.updateGeometry()

    def currentIndex(self) -> int:
        """Return the index of the highlighted segment."""

        return self._index

    def setCurrentIndex(self, index: int, animate: bool = True) -> None:
        """Select *index*, optionally animating the highlight transition."""

        clamped = max(0, min(index, len(self._items) - 1))
        if clamped == self._index:
            return
        start = float(self._index)
        self._index = clamped
        end = float(self._index)
        if animate:
            self._anim.stop()
            self._anim.setStartValue(start)
            self._anim.setEndValue(end)
            self._anim.start()
        else:
            self._anim_pos = end
            self.update()
        self.currentIndexChanged.emit(self._index)

    def getAnimPos(self) -> float:
        """Return the current highlight animation progress."""

        return self._anim_pos

    def setAnimPos(self, value: float) -> None:
        """Update the highlight animation progress and repaint the control."""

        self._anim_pos = value
        self.update()

    animPos = Property(float, getAnimPos, setAnimPos)

    # ------------------------------------------------------------------
    # Input handling helpers
    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self._index_from_x(event.position().x())
        if index is not None:
            self.setCurrentIndex(index, animate=True)

    def keyPressEvent(self, event):  # type: ignore[override]
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_A):
            self.setCurrentIndex(self._index - 1)
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_D):
            self.setCurrentIndex(self._index + 1)
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Painting helpers
    def paintEvent(self, _):  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        width = self.width()
        height = self.height()
        overdraw = max(1.0, self.devicePixelRatioF() * 0.8)

        outer = _align_rect_05(QRectF(1.0, 1.0, width - 2.0, height - 2.0))
        outer_path = QPainterPath()
        outer_path.addRoundedRect(outer, self.radius, self.radius)

        bg_rect = outer.adjusted(-overdraw, -overdraw, overdraw, overdraw)
        bg_path = QPainterPath()
        bg_path.addRoundedRect(bg_rect, self.radius + overdraw, self.radius + overdraw)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.bg)
        painter.drawPath(bg_path)

        inner = _align_rect_05(outer.adjusted(self.h_pad, self.v_pad, -self.h_pad, -self.v_pad))
        segment_rects, separators = self._segment_rects_and_boundaries(inner)

        if segment_rects:
            band_rect = self._lerp_rects(segment_rects, self._anim_pos)

            leftmost = self._anim_pos < 0.001
            rightmost = self._anim_pos > len(segment_rects) - 1.001
            band_rect = QRectF(
                band_rect.left() - (self.h_pad if leftmost else overdraw),
                outer.top() - overdraw,
                band_rect.width()
                + (2 * overdraw if not (leftmost or rightmost) else self.h_pad + overdraw),
                outer.height() + 2 * overdraw,
            )
            band_rect = _align_rect_05(band_rect)

            band_path = QPainterPath()
            band_path.addRoundedRect(band_rect, self.radius, self.radius)
            selection_path = outer_path.intersected(band_path)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.frosty_a)
            painter.drawPath(selection_path)

            gradient = QLinearGradient(band_rect.topLeft(), band_rect.bottomLeft())
            gradient.setColorAt(0.0, self.frosty_b)
            gradient.setColorAt(0.6, QColor(255, 255, 255, 0))
            painter.setBrush(gradient)
            painter.drawPath(selection_path)

        painter.setPen(QPen(QColor(90, 90, 90), self.sep_width))
        y1 = _snap05(inner.top() + self.sep_inset)
        y2 = _snap05(inner.bottom() - self.sep_inset)
        for x in separators:
            x_aligned = _snap05(x)
            painter.drawLine(QPointF(x_aligned, y1), QPointF(x_aligned, y2))

        for index, rect in enumerate(segment_rects):
            font = QFont(self.font())
            font.setBold(index == round(self._anim_pos))
            painter.setFont(font)
            painter.setPen(self.text_active if index == round(self._anim_pos) else self.text_inactive)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._items[index])

        painter.setPen(QPen(self.border, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(outer_path)

    # ------------------------------------------------------------------
    # Geometry helpers
    def _segment_rects_and_boundaries(self, inner: QRectF) -> tuple[list[QRectF], list[float]]:
        count = len(self._items)
        if count <= 0:
            return [], []
        width_each = inner.width() / count
        rects: list[QRectF] = []
        boundaries: list[float] = []
        for index in range(count):
            x = inner.left() + index * width_each
            rects.append(QRectF(x, inner.top(), width_each, inner.height()))
            if index > 0:
                boundaries.append(x)
        usable: list[QRectF] = []
        for index, rect in enumerate(rects):
            left_bound = inner.left() if index == 0 else boundaries[index - 1] + self.sep_width / 2
            right_bound = inner.right() if index == count - 1 else boundaries[index] - self.sep_width / 2
            usable.append(QRectF(left_bound, inner.top(), right_bound - left_bound, inner.height()))
        return usable, boundaries

    def _index_from_x(self, x: float) -> int | None:
        inner = QRectF(self.h_pad, self.v_pad, self.width() - 2 * self.h_pad, self.height() - 2 * self.v_pad)
        count = len(self._items)
        if count <= 0:
            return None
        index = int((x - inner.left()) // (inner.width() / count))
        return index if 0 <= index < count else None

    @staticmethod
    def _lerp(a: float, b: float, t: float) -> float:
        return a + (b - a) * t

    def _lerp_rects(self, rects: Sequence[QRectF], pos: float) -> QRectF:
        count = len(rects)
        if count == 1 or pos <= 0:
            return rects[0]
        if pos >= count - 1:
            return rects[-1]
        index = int(pos)
        fraction = pos - index
        first = rects[index]
        second = rects[index + 1]
        x = self._lerp(first.left(), second.left(), fraction)
        width = self._lerp(first.width(), second.width(), fraction)
        return QRectF(x, first.top(), width, first.height())

    # ------------------------------------------------------------------
    # Sizing helpers
    def sizeHint(self) -> QSize:  # type: ignore[override]
        """Return the preferred control size based on the label lengths."""

        segment_width = self._segment_width_hint()
        count = max(1, len(self._items))
        total_width = int(round(2 * self.h_pad + segment_width * count))
        return QSize(total_width, self.height_hint)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        """Mirror :meth:`sizeHint` so layouts never collapse the control."""

        return self.sizeHint()

    def _segment_width_hint(self) -> float:
        """Estimate a comfortable width for an individual segment."""

        metrics = self.fontMetrics()
        if not self._items:
            text_width = metrics.horizontalAdvance("Item")
        else:
            text_width = max(metrics.horizontalAdvance(text) for text in self._items)
        # Provide additional breathing room around the text to mimic the native Photos toolbar.
        # The constant accounts for the inner padding used during painting so the highlight band
        # never clips the glyphs even when translated to high-DPI surfaces.
        return float(text_width + (self.sep_inset + self.h_pad))


# ----------------------------------------------------------------------
# Demo harness ---------------------------------------------------------
if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    root = QWidget()
    layout = QVBoxLayout(root)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(16)
    bar = SegmentedTopBar(["Adjust", "Filters", "Crop"])
    bar.setFixedHeight(44)
    layout.addWidget(bar)
    bar2 = SegmentedTopBar(["Basic", "Color", "Details", "Optics"])
    bar2.setFixedHeight(44)
    layout.addWidget(bar2)
    root.setStyleSheet("QWidget { background: #1f1f1f; }")
    root.resize(560, 180)
    root.setWindowTitle("Segmented Top Bar – round highlight covers outer corners")
    root.show()
    sys.exit(app.exec())
