"""Custom slider used by the edit sidebar."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget


class BWSlider(QWidget):
    """Horizontal slider that renders a split-tone track and bold labels."""

    valueChanged = Signal(float)
    """Emitted whenever the slider's value changes."""

    def __init__(
        self,
        name: str = "Intensity",
        parent: QWidget | None = None,
        *,
        minimum: float = 0.0,
        maximum: float = 1.0,
        initial: Optional[float] = None,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        if self._maximum <= self._minimum:
            self._maximum = self._minimum + 1.0
        self._value = float(initial) if initial is not None else (self._minimum + self._maximum) / 2.0
        self._value = self._clamp(self._value)
        self._dragging = False
        self._hover = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Geometry parameters ------------------------------------------------
        self.setMinimumHeight(35)
        self.setMinimumWidth(260)
        self.track_height = 30
        self.radius = 10
        self.h_padding = 14
        self.line_width = 3

        # Colour palette tuned for the dark edit theme -----------------------
        self.c_left = QColor(132, 132, 132)
        self.c_right = QColor(54, 54, 54)
        self.c_bg = QColor(42, 42, 42)
        self.c_line = QColor(0, 122, 255)
        self.c_text = QColor(235, 235, 235)

    # ------------------------------------------------------------------
    # Public API
    def value(self) -> float:
        """Return the current slider value within the configured range."""

        return self._value

    def setValue(self, value: float, emit: bool = True) -> None:
        """Update the slider to *value* and optionally emit :attr:`valueChanged`."""

        clamped = self._clamp(value)
        if abs(clamped - self._value) <= 1e-6:
            return
        self._value = clamped
        self.update()
        if emit:
            self.valueChanged.emit(self._value)

    def setName(self, name: str) -> None:
        """Change the label rendered on the left side of the track."""

        self._name = name
        self.update()

    def setRange(self, minimum: float, maximum: float) -> None:
        """Adjust the admissible value range and clamp the current value."""

        self._minimum = float(minimum)
        self._maximum = float(maximum)
        if self._maximum <= self._minimum:
            self._maximum = self._minimum + 1.0
        self.setValue(self._value, emit=False)

    # ------------------------------------------------------------------
    # Event handlers
    def enterEvent(self, _):  # type: ignore[override]
        self._hover = True
        self.update()

    def leaveEvent(self, _):  # type: ignore[override]
        self._hover = False
        self.update()

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._set_by_pos(event.position().x())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if self._dragging:
            self._set_by_pos(event.position().x())

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.unsetCursor()

    def wheelEvent(self, event):  # type: ignore[override]
        step = (self._maximum - self._minimum) * 0.01
        delta = event.angleDelta().y() / 120.0
        self.setValue(self._value + delta * step)

    def keyPressEvent(self, event):  # type: ignore[override]
        step = (self._maximum - self._minimum) * 0.01
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_A):
            self.setValue(self._value - step)
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_D):
            self.setValue(self._value + step)
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Rendering helpers
    def paintEvent(self, _):  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        width = self.width()
        height = self.height()
        track_height = self.track_height
        track_rect = QRectF(self.h_padding, (height - track_height) / 2, width - 2 * self.h_padding, track_height)
        x_line = track_rect.left() + self._normalised_value() * track_rect.width()

        round_path = QPainterPath()
        round_path.addRoundedRect(track_rect, self.radius, self.radius)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.c_bg)
        painter.drawPath(round_path)

        painter.save()
        painter.setClipPath(round_path)
        painter.setClipRect(
            QRectF(track_rect.left(), track_rect.top(), max(0.0, x_line - track_rect.left()), track_height),
            Qt.IntersectClip,
        )
        painter.fillRect(track_rect, self.c_left)
        painter.restore()

        painter.save()
        painter.setClipPath(round_path)
        painter.setClipRect(
            QRectF(x_line, track_rect.top(), max(0.0, track_rect.right() - x_line), track_height),
            Qt.IntersectClip,
        )
        painter.fillRect(track_rect, self.c_right)
        painter.restore()

        painter.save()
        painter.setClipPath(round_path)
        pen = QPen(self.c_line)
        pen.setWidth(self.line_width)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(x_line, track_rect.top()), QPointF(x_line, track_rect.bottom()))
        painter.restore()

        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self.c_text)

        left_rect = QRectF(track_rect.left() + 10, track_rect.top(), track_rect.width() / 2 - 12, track_height)
        right_rect = QRectF(track_rect.center().x(), track_rect.top(), track_rect.width() / 2 - 10, track_height)
        painter.drawText(left_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._name)
        painter.drawText(right_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{self._value:.2f}")

        if self._hover and not self._dragging:
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    # ------------------------------------------------------------------
    # Internal helpers
    def _normalised_value(self) -> float:
        span = self._maximum - self._minimum
        if span <= 0:
            return 0.0
        return (self._value - self._minimum) / span

    def _clamp(self, value: float) -> float:
        return max(self._minimum, min(self._maximum, float(value)))

    def _set_by_pos(self, x: float) -> None:
        left = self.h_padding
        right = self.width() - self.h_padding
        if right <= left:
            return
        ratio = (x - left) / (right - left)
        span = self._maximum - self._minimum
        self.setValue(self._minimum + ratio * span)


# Demo harness ----------------------------------------------------------
if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    window = QWidget()
    layout = QVBoxLayout(window)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(12)
    for label, value in [("Intensity", 0.50), ("Neutrals", 0.00), ("Tone", 0.00), ("Grain", 0.00)]:
        slider = BWSlider(label, minimum=-1.0, maximum=1.0, initial=value)
        slider.setValue(value, emit=False)
        layout.addWidget(slider)
    window.setWindowTitle("B&W Slider (bold text, clipped thumb)")
    window.resize(560, 240)
    window.show()
    sys.exit(app.exec())
