"""Widget that displays a scaled image while preserving aspect ratio."""

from __future__ import annotations

from typing import Optional, Tuple, cast

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class ImageViewer(QWidget):
    """Simple viewer that centers, zooms, and scrolls a ``QPixmap``."""

    replayRequested = Signal()
    """Emitted when the user clicks the still frame to replay a Live Photo."""

    zoomChanged = Signal(float)
    """Emitted whenever the zoom factor changes via UI or programmatic control."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Ignored,
        )

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(False)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll_area.setStyleSheet("background-color: black; border: none;")
        self._scroll_area.setWidget(self._label)
        self._scroll_area.viewport().installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._scroll_area)

        self.setStyleSheet("background-color: black;")

        self._live_replay_enabled = False
        self._zoom_factor = 1.0
        self._min_zoom = 0.1
        self._max_zoom = 4.0
        self._button_step = 0.1
        self._wheel_step = 0.1
        self._base_size: Optional[QSize] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        """Display *pixmap* and update the scaled rendering."""

        self._pixmap = pixmap
        if self._pixmap is None or self._pixmap.isNull():
            self._label.clear()
            self._label.setMinimumSize(0, 0)
            self._base_size = None
            self._zoom_factor = 1.0
            self.zoomChanged.emit(self._zoom_factor)
            return

        self._zoom_factor = 1.0
        self._render_pixmap()
        self.zoomChanged.emit(self._zoom_factor)

    def clear(self) -> None:
        """Remove any currently displayed image."""

        self._pixmap = None
        self._label.clear()
        self._label.setMinimumSize(0, 0)
        self._base_size = None
        self._zoom_factor = 1.0
        self.zoomChanged.emit(self._zoom_factor)

    def set_live_replay_enabled(self, enabled: bool) -> None:
        """Allow emitting replay requests when the still frame is shown."""

        self._live_replay_enabled = bool(enabled)

    def set_zoom(self, factor: float, *, anchor: Optional[QPoint] = None) -> None:
        """Set the zoom *factor* relative to the fit-to-window baseline."""

        clamped = max(self._min_zoom, min(self._max_zoom, float(factor)))
        if abs(clamped - self._zoom_factor) < 1e-3:
            return

        anchor_ratios: Optional[Tuple[float, float]] = None
        if (
            anchor is not None
            and self._pixmap is not None
            and not self._pixmap.isNull()
            and self._label.width() > 0
            and self._label.height() > 0
        ):
            anchor_ratios = self._capture_anchor_ratios(anchor)

        self._zoom_factor = clamped
        if self._pixmap is not None and not self._pixmap.isNull():
            self._render_pixmap(anchor_point=anchor, anchor_ratios=anchor_ratios)
        self.zoomChanged.emit(self._zoom_factor)

    def reset_zoom(self) -> None:
        """Return the zoom factor to ``1.0`` (fit to window)."""

        self._zoom_factor = 1.0
        if self._pixmap is not None and not self._pixmap.isNull():
            self._render_pixmap()
        self.zoomChanged.emit(self._zoom_factor)

    def zoom_in(self) -> None:
        """Increase the zoom factor using the standard toolbar step."""

        if self._pixmap is None or self._pixmap.isNull():
            return
        self._step_zoom(self._button_step)

    def zoom_out(self) -> None:
        """Decrease the zoom factor using the standard toolbar step."""

        if self._pixmap is None or self._pixmap.isNull():
            return
        self._step_zoom(-self._button_step)

    def zoom_factor(self) -> float:
        """Return the currently applied zoom factor."""

        return self._zoom_factor

    def viewport_center(self) -> QPoint:
        """Return the centre point of the scroll area's viewport."""

        return self._scroll_area.viewport().rect().center()

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._pixmap is not None:
            anchor = self.viewport_center()
            anchor_ratios = self._capture_anchor_ratios(anchor)
            self._render_pixmap(anchor_point=anchor, anchor_ratios=anchor_ratios)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # pragma: no cover - GUI behaviour
        if self._live_replay_enabled and event.button() == Qt.MouseButton.LeftButton:
            self.replayRequested.emit()
        super().mousePressEvent(event)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self._scroll_area.viewport() and event.type() == QEvent.Type.Wheel:
            if self._pixmap is None or self._pixmap.isNull():
                return False
            wheel_event = cast(QWheelEvent, event)
            if self._handle_wheel_event(wheel_event):
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _render_pixmap(
        self,
        *,
        anchor_point: Optional[QPoint] = None,
        anchor_ratios: Optional[Tuple[float, float]] = None,
    ) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            self._label.clear()
            self._label.setMinimumSize(0, 0)
            self._base_size = None
            return

        viewport_size = self._scroll_area.viewport().size()
        if not viewport_size.isValid() or viewport_size.isEmpty():
            return

        pix_size = self._pixmap.size()
        if pix_size.isEmpty():
            self._label.clear()
            self._label.setMinimumSize(0, 0)
            self._base_size = None
            return

        width_ratio = viewport_size.width() / max(1, pix_size.width())
        height_ratio = viewport_size.height() / max(1, pix_size.height())
        base_scale = min(width_ratio, height_ratio)
        if base_scale <= 0:
            base_scale = 1.0

        fit_width = max(1, int(round(pix_size.width() * base_scale)))
        fit_height = max(1, int(round(pix_size.height() * base_scale)))
        self._base_size = QSize(fit_width, fit_height)

        scale = base_scale * self._zoom_factor
        target_width = max(1, int(round(pix_size.width() * scale)))
        target_height = max(1, int(round(pix_size.height() * scale)))

        scaled = self._pixmap.scaled(
            QSize(target_width, target_height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())
        self._label.setMinimumSize(scaled.size())

        h_bar = self._scroll_area.horizontalScrollBar()
        v_bar = self._scroll_area.verticalScrollBar()
        if anchor_point is not None and anchor_ratios is not None:
            self._restore_anchor(anchor_point, anchor_ratios, h_bar, v_bar)
        else:
            self._center_viewport(h_bar, v_bar)

    def _capture_anchor_ratios(self, anchor: QPoint) -> Tuple[float, float]:
        h_bar = self._scroll_area.horizontalScrollBar()
        v_bar = self._scroll_area.verticalScrollBar()
        content_width = max(1, self._label.width())
        content_height = max(1, self._label.height())
        rel_x = (h_bar.value() + anchor.x()) / content_width
        rel_y = (v_bar.value() + anchor.y()) / content_height
        return (
            max(0.0, min(rel_x, 1.0)),
            max(0.0, min(rel_y, 1.0)),
        )

    def _restore_anchor(
        self,
        anchor_point: QPoint,
        anchor_ratios: Tuple[float, float],
        h_bar: QScrollBar,
        v_bar: QScrollBar,
    ) -> None:
        rel_x, rel_y = anchor_ratios
        content_width = max(1, self._label.width())
        content_height = max(1, self._label.height())

        target_x = int(round(rel_x * content_width - anchor_point.x()))
        target_y = int(round(rel_y * content_height - anchor_point.y()))

        h_bar.setValue(max(h_bar.minimum(), min(target_x, h_bar.maximum())))
        v_bar.setValue(max(v_bar.minimum(), min(target_y, v_bar.maximum())))

    def _center_viewport(self, h_bar: QScrollBar, v_bar: QScrollBar) -> None:
        for bar in (h_bar, v_bar):
            span = bar.maximum() - bar.minimum()
            if span > 0:
                bar.setValue(bar.minimum() + span // 2)
            else:
                bar.setValue(bar.minimum())

    def _handle_wheel_event(self, event: QWheelEvent) -> bool:
        angle = event.angleDelta().y()
        if angle == 0:
            return False

        step = self._wheel_step if angle > 0 else -self._wheel_step
        anchor = event.position().toPoint()
        self.set_zoom(self._zoom_factor + step, anchor=anchor)
        event.accept()
        return True

    def _step_zoom(self, delta: float) -> None:
        anchor = self.viewport_center()
        self.set_zoom(self._zoom_factor + delta, anchor=anchor)
