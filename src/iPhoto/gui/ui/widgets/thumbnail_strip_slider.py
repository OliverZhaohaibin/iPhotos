"""Slider that displays adjustment previews using thumbnail strips."""

from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QPointF, QRectF, QSize, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
    QGraphicsOpacityEffect,
)

from ....core.image_filters import apply_adjustments
from ....core.light_resolver import resolve_light_vector


class ThumbnailStripSlider(QFrame):
    """Render a slider with thumbnails representing different adjustment strengths."""

    valueChanged = Signal(float)
    valueCommitted = Signal(float)

    clickedWhenDisabled = Signal()

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
        self._tick_values: List[float] = []
        self._thumbnails: List[Optional[QPixmap]] = []
        self._placeholder_cache: dict[int, QPixmap] = {}
        self._generation = 0
        self._preview_generator: Callable[[QImage, float], QImage] = self._generate_light_preview

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
        self._track_frame.clickedWhenDisabled.connect(self.clickedWhenDisabled)
        self.setMinimumHeight(self._track_height + label_height + 12)

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)

        # Initialise the placeholder state so the painter can immediately render the
        # slider without waiting for background thumbnails to arrive.
        self._reset_thumbnails()

    # ------------------------------------------------------------------
    def set_label(self, text: str) -> None:
        """Update the caption rendered above the track."""

        if self._label_widget is not None:
            self._label_widget.setText(text)

    def setImage(self, image: QImage | QPixmap | None) -> None:
        """Assign *image* as the base preview used for thumbnail generation."""

        if isinstance(image, QPixmap):
            image = image.toImage()
        self._bump_generation()
        if image is None or image.isNull():
            self._base_image = None
            self._placeholder_cache.clear()
            self._reset_thumbnails()
            self._track_frame.update()
            return
        self._base_image = image.convertToFormat(QImage.Format.Format_ARGB32)
        self._placeholder_cache.clear()
        self._reset_thumbnails()
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
        """Keep the frame enabled to capture clicks, but disable the visual track."""
        super().setEnabled(True)  # 保持 QFrame 启用
        if self._label_widget is not None:
            self._label_widget.setEnabled(enabled)
        self._track_frame.setEnabled(enabled)
        self._opacity_effect.setOpacity(1.0 if enabled else 0.5)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle clicks when the track is disabled to re-enable it."""
        if event.button() == Qt.MouseButton.LeftButton:
            if (
                not self._track_frame.isEnabled()
                and self._track_frame.geometry().contains(event.position().toPoint())
            ):
                # 发出信号，通知父级 (EditLightSection)
                self.clickedWhenDisabled.emit()

                # 手动将点击事件转发给现在已启用的 track 控件
                track_event = QMouseEvent(
                    event.type(),
                    self._track_frame.mapFrom(self, event.position().toPoint()),
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                QApplication.sendEvent(self._track_frame, track_event)
                event.accept()
                return
        super().mousePressEvent(event)

    # ------------------------------------------------------------------
    def set_preview_generator(self, generator: Callable[[QImage, float], QImage]) -> None:
        """Set the callable used to generate preview thumbnails."""

        self._preview_generator = generator
        self._bump_generation()
        self._reset_thumbnails()
        self._track_frame.update()

    # ------------------------------------------------------------------
    def tick_values(self) -> List[float]:
        """Return the slider values represented by each thumbnail slot."""

        return list(self._tick_values)

    def generation_id(self) -> int:
        """Return the current thumbnail generation token."""

        return self._generation

    def track_height(self) -> int:
        """Expose the visual height of the thumbnail strip for worker scaling."""

        return self._track_height

    def preview_generator(self) -> Callable[[QImage, float], QImage]:
        """Return the callable used to produce adjustment previews."""

        return self._preview_generator

    def base_image(self) -> Optional[QImage]:
        """Return a detached copy of the currently assigned base preview image."""

        if self._base_image is None or self._base_image.isNull():
            return None
        return QImage(self._base_image)

    @Slot(int, QImage, int)
    def update_thumbnail(self, index: int, image: QImage, generation: int) -> None:
        """Apply an updated thumbnail delivered from the background worker."""

        if generation != self._generation:
            # A newer generation is already in-flight; ignore stale results.
            return
        if index < 0 or index >= len(self._thumbnails):
            return
        if image.isNull():
            self._thumbnails[index] = None
        else:
            self._thumbnails[index] = QPixmap.fromImage(image)
        self._track_frame.update()

    def _clamp(self, value: float) -> float:
        return max(self._minimum, min(self._maximum, float(value)))

    def _normalise(self, value: float) -> float:
        span = self._maximum - self._minimum
        if span <= 0:
            return 0.0
        return (value - self._minimum) / span

    def _reset_thumbnails(self) -> None:
        """Clear the cached thumbnail pixmaps while keeping slot counts consistent."""

        self._tick_values = self._compute_tick_values()
        self._thumbnails = [None for _ in self._tick_values]

    def _compute_tick_values(self) -> List[float]:
        """Derive evenly spaced slider values for the configured tick count."""

        if self._tick_count <= 1:
            return [self._minimum]
        step = (self._maximum - self._minimum) / (self._tick_count - 1)
        return [self._minimum + i * step for i in range(self._tick_count)]

    def _bump_generation(self) -> int:
        """Advance the generation counter to invalidate stale thumbnail updates."""

        self._generation += 1
        return self._generation

    def _ensure_placeholder_pixmap(self, height: int) -> QPixmap:
        """Return a cached placeholder pixmap scaled to *height* pixels tall."""

        height = max(1, int(height))
        cached = self._placeholder_cache.get(height)
        if cached is not None:
            return cached
        if self._base_image is not None and not self._base_image.isNull():
            scaled = self._base_image.scaledToHeight(
                height,
                Qt.TransformationMode.SmoothTransformation,
            )
            pixmap = QPixmap.fromImage(scaled)
        else:
            width = max(1, int(height * 1.5))
            pixmap = QPixmap(width, height)
            placeholder_color = self.palette().base().color().darker(115)
            pixmap.fill(placeholder_color)
        self._placeholder_cache[height] = pixmap
        return pixmap

    @staticmethod
    def _generate_light_preview(image: QImage, value: float) -> QImage:
        adjustments = resolve_light_vector(value, None)
        return apply_adjustments(image, adjustments)


class _ThumbnailTrack(QWidget):
    """Internal widget handling painting and mouse interaction for the slider."""

    # [Gemini] 添加信号
    clickedWhenDisabled = Signal()

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

        thumbnails = slider._thumbnails
        if thumbnails:
            segment_width = rect.width() / len(thumbnails)
            placeholder = slider._ensure_placeholder_pixmap(int(rect.height()))
            painter.save()
            painter.setClipPath(path)
            x = rect.left()
            for pixmap in thumbnails:
                current = pixmap or placeholder
                if not current.isNull():
                    target = QRectF(x, rect.top(), segment_width, rect.height())
                    pixmap_ratio = current.width() / max(1.0, current.height())
                    target_ratio = target.width() / max(1.0, target.height())
                    if pixmap_ratio > target_ratio:
                        height = current.height()
                        width = int(height * target_ratio)
                        sx = max(0, (current.width() - width) // 2)
                        source = QRectF(sx, 0, width, height)
                    else:
                        width = current.width()
                        height = int(width / target_ratio)
                        sy = max(0, (current.height() - height) // 2)
                        source = QRectF(0, sy, width, height)
                    painter.drawPixmap(target, current, source)
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
    # [Gemini] 修改 mousePressEvent
    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton or self._slider is None:
            super().mousePressEvent(event)
            return

        if not self.isEnabled():
            # 控件被禁用了，发出信号
            self.clickedWhenDisabled.emit()
            event.accept()
            return

        self._slider._pressed = True
        self._update_value_from_position(event.position().x())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._slider is None or not self._slider._pressed:
            super().mouseMoveEvent(event)
            return
        self._update_value_from_position(event.position().x())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if (
            self._slider is None
            or not self._slider._pressed
            or event.button() != Qt.MouseButton.LeftButton
        ):
            super().mouseReleaseEvent(event)
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