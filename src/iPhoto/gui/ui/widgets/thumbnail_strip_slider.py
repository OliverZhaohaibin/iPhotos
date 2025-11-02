"""Slider that displays adjustment previews using thumbnail strips."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from PySide6.QtCore import Qt, QPointF, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from ....core.image_filters import apply_adjustments
from ....core.light_resolver import resolve_light_vector


@dataclass
class _TickPreview:
    """Store a preview pixmap for a particular slider value."""

    value: float
    pixmap: QPixmap


class ThumbnailStripSlider(QFrame):
    """Render a slider with thumbnails representing different adjustment strengths."""

    valueChanged = Signal(float)
    valueCommitted = Signal(float)

    def __init__(
        self,
        label: Optional[str] = None,
        parent: Optional[QWidget] = None,
        *,
        minimum: float = -1.0,
        maximum: float = 1.0,
        initial: float = 0.0,
        tick_count: int = 7,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("thumbnailStripSlider")
        self.setMouseTracking(True)

        self._minimum = float(minimum)
        self._maximum = float(maximum)
        if self._maximum <= self._minimum:
            self._maximum = self._minimum + 1.0
        self._value = self._clamp(initial)

        self._tick_count = max(3, int(tick_count))
        self._track_height = 56
        self._corner_radius = 8.0
        self._pressed = False

        self._base_image: Optional[QImage] = None
        self._scaled: Optional[QImage] = None
        self._scaled_height = 0
        self._tick_previews: List[_TickPreview] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        label_height = 0
        if label:
            self._label_widget = QLabel(label, self)
            self._label_widget.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
            )
            self._label_widget.setStyleSheet("QLabel { font-weight: 600; }")
            layout.addWidget(self._label_widget)
            label_height = self._label_widget.sizeHint().height() + 6
        else:
            self._label_widget: Optional[QLabel] = None

        self._track_frame = _ThumbnailTrack(self)
        layout.addWidget(self._track_frame)
        layout.setStretchFactor(self._track_frame, 1)

        self._track_frame.install_slider(self)
        self.setMinimumHeight(self._track_height + label_height + 12)

    # ------------------------------------------------------------------
    def set_label(self, text: str) -> None:
        """Update the caption rendered above the track."""

        if self._label_widget is not None:
            self._label_widget.setText(text)

    def setImage(self, image: QImage | QPixmap | None) -> None:
        """Assign *image* as the base preview used for thumbnail generation."""

        if isinstance(image, QPixmap):
            image = image.toImage()
        if image is None or image.isNull():
            self._base_image = None
            self._scaled = None
            self._tick_previews.clear()
            self._track_frame.update()
            return
        self._base_image = image.convertToFormat(QImage.Format.Format_ARGB32)
        self._scaled = None
        self._tick_previews.clear()
        self._track_frame.update()

    def setValue(self, value: float, *, emit: bool = True) -> None:
        """Update the slider to *value* and optionally notify listeners."""

        clamped = self._clamp(value)
        if abs(clamped - self._value) <= 1e-6:
            return
        self._value = clamped
        self._track_frame.update()
        if emit:
            self.valueChanged.emit(self._value)

    def value(self) -> float:
        """Return the current slider value."""

        return self._value

    def update_from_value(self, value: float) -> None:
        """Synchronise the slider position with *value* without emitting signals."""

        block = self.blockSignals(True)
        try:
            self.setValue(value, emit=False)
        finally:
            self.blockSignals(block)

    def setEnabled(self, enabled: bool) -> None:  # type: ignore[override]
        super().setEnabled(enabled)
        if self._label_widget is not None:
            self._label_widget.setEnabled(enabled)
        self._track_frame.setEnabled(enabled)
        self._track_frame.update()

    # ------------------------------------------------------------------
    def _clamp(self, value: float) -> float:
        return max(self._minimum, min(self._maximum, float(value)))

    def _normalise(self, value: float) -> float:
        span = self._maximum - self._minimum
        if span <= 0:
            return 0.0
        return (value - self._minimum) / span

    def _ensure_scaled(self, height: int) -> None:
        if self._base_image is None:
            return
        if self._scaled is not None and self._scaled_height == height:
            return
        self._scaled = self._base_image.scaledToHeight(
            height,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scaled_height = height
        self._tick_previews.clear()

    def _ensure_tick_previews(self, track_rect: QRectF) -> None:
        if self._base_image is None:
            return
        self._ensure_scaled(int(track_rect.height()))
        if self._scaled is None or self._tick_previews:
            return
        values = [
            self._minimum + i * (self._maximum - self._minimum) / (self._tick_count - 1)
            for i in range(self._tick_count)
        ]
        for value in values:
            preview = self._generate_preview(self._scaled, value)
            self._tick_previews.append(
                _TickPreview(value, QPixmap.fromImage(preview))
            )

    def _generate_preview(self, image: QImage, value: float) -> QImage:
        """Return an adjusted preview using *image* and the Light resolver."""

        adjustments = resolve_light_vector(value, None)
        return apply_adjustments(image, adjustments)


class _ThumbnailTrack(QWidget):
    """Internal widget handling painting and mouse interaction for the slider."""

    def __init__(self, parent: ThumbnailStripSlider) -> None:
        super().__init__(parent)
        self._slider: Optional[ThumbnailStripSlider] = None
        self.setMouseTracking(True)

    def install_slider(self, slider: ThumbnailStripSlider) -> None:
        """Attach *slider* so this track can query state and emit updates."""

        self._slider = slider

    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:  # type: ignore[override]
        if self._slider is None:
            return QSize(320, 56)
        return QSize(320, self._slider._track_height)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return self.sizeHint()

    # ------------------------------------------------------------------
    def paintEvent(self, _) -> None:  # type: ignore[override]
        if self._slider is None:
            return
        slider = self._slider
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        margin = 8
        rect = QRectF(
            margin,
            (self.height() - slider._track_height) / 2,
            max(0.0, self.width() - 2 * margin),
            slider._track_height,
        )
        path = QPainterPath()
        path.addRoundedRect(rect, slider._corner_radius, slider._corner_radius)

        painter.setPen(Qt.PenStyle.NoPen)
        base_color = self.palette().base().color().darker(110)
        painter.setBrush(base_color)
        painter.drawPath(path)

        slider._ensure_tick_previews(rect)
        if slider._tick_previews:
            segment_width = rect.width() / len(slider._tick_previews)
            painter.save()
            painter.setClipPath(path)
            x = rect.left()
            for preview in slider._tick_previews:
                pixmap = preview.pixmap
                if not pixmap.isNull():
                    target = QRectF(x, rect.top(), segment_width, rect.height())
                    pixmap_ratio = pixmap.width() / max(1.0, pixmap.height())
                    target_ratio = target.width() / max(1.0, target.height())
                    if pixmap_ratio > target_ratio:
                        height = pixmap.height()
                        width = int(height * target_ratio)
                        sx = max(0, (pixmap.width() - width) // 2)
                        source = QRectF(sx, 0, width, height)
                    else:
                        width = pixmap.width()
                        height = int(width / target_ratio)
                        sy = max(0, (pixmap.height() - height) // 2)
                        source = QRectF(0, sy, width, height)
                    painter.drawPixmap(target, pixmap, source)
                x += segment_width
            painter.restore()

        mid_pen = QPen(self.palette().base().color().lighter(160), 1.0)
        painter.setPen(mid_pen)
        mid_x = rect.center().x()
        painter.drawLine(QPointF(mid_x, rect.top()), QPointF(mid_x, rect.bottom()))

        normalised = slider._normalise(slider._value)
        center_x = rect.left() + normalised * rect.width()
        handle_width = 4
        handle_height = rect.height() + 10
        handle_rect = QRectF(
            center_x - handle_width / 2,
            rect.center().y() - handle_height / 2,
            handle_width,
            handle_height,
        )
        highlight = QColor(self.palette().highlight().color())
        highlight.setAlpha(220)
        painter.setBrush(highlight)
        painter.setPen(QPen(Qt.GlobalColor.white, 1.0))
        painter.drawRoundedRect(handle_rect, 2.0, 2.0)

    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton or self._slider is None:
            return
        self._slider._pressed = True
        self._update_value_from_position(event.position().x())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._slider is None or not self._slider._pressed:
            return
        self._update_value_from_position(event.position().x())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if (
            self._slider is None
            or not self._slider._pressed
            or event.button() != Qt.MouseButton.LeftButton
        ):
            return
        self._slider._pressed = False
        self._update_value_from_position(event.position().x())
        self._slider.valueCommitted.emit(self._slider._value)
        event.accept()

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        if self._slider is not None and not self._slider._pressed:
            self.unsetCursor()
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    def _update_value_from_position(self, x: float) -> None:
        if self._slider is None:
            return
        margin = 8
        width = max(0.0, self.width() - 2 * margin)
        if width <= 0:
            return
        progress = (x - margin) / width
        progress = max(0.0, min(1.0, progress))
        value = self._slider._minimum + progress * (
            self._slider._maximum - self._slider._minimum
        )
        self._slider.setValue(value)