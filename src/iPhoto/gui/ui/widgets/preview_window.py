"""Lightweight floating window that previews a video asset."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QRegion, QResizeEvent
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QVBoxLayout, QWidget, QSizePolicy

from ....config import (
    PREVIEW_WINDOW_CLOSE_DELAY_MS,
    PREVIEW_WINDOW_CORNER_RADIUS,
    PREVIEW_WINDOW_DEFAULT_WIDTH,
    PREVIEW_WINDOW_MUTED,
)
from ..media import MediaController, require_multimedia

if importlib.util.find_spec("PySide6.QtMultimediaWidgets") is not None:
    from PySide6.QtMultimediaWidgets import QVideoWidget
else:  # pragma: no cover - requires optional Qt module
    QVideoWidget = None  # type: ignore[assignment]


class _VideoWidget(QVideoWidget):
    """Video surface that notifies the parent when resized."""

    def __init__(self, on_resize: Callable[[], None], parent: Optional[QWidget] = None) -> None:
        if QVideoWidget is None:  # pragma: no cover - optional Qt module
            raise RuntimeError(
                "PySide6.QtMultimediaWidgets is unavailable; install PySide6 with "
                "QtMultimediaWidgets support to enable video previews."
            )
        super().__init__(parent)
        self._on_resize = on_resize
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._on_resize()


class _PreviewFrame(QWidget):
    """Draws rounded chrome around the embedded video widget."""

    def __init__(self, corner_radius: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._corner_radius = max(0, corner_radius)
        self._border_width = 2
        self._background = QColor(18, 18, 22)
        self._border = QColor(255, 255, 255, 28)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            self._border_width,
            self._border_width,
            self._border_width,
            self._border_width,
        )
        layout.setSpacing(0)

        self._video_widget = _VideoWidget(self._update_masks, self)
        layout.addWidget(self._video_widget)

        self._update_masks()

    def video_widget(self) -> QVideoWidget:
        return self._video_widget

    def set_corner_radius(self, corner_radius: int) -> None:
        radius = max(0, corner_radius)
        if radius == self._corner_radius:
            return
        self._corner_radius = radius
        self._update_masks()
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(self._corner_radius)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._background)
        painter.drawPath(path)

        if self._border_width > 0 and self._border.alpha() > 0:
            pen = QPen(self._border)
            pen.setWidth(self._border_width)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_masks()

    def _update_masks(self) -> None:
        if self.width() <= 0 or self.height() <= 0:
            self.clearMask()
            self._video_widget.clearMask()
            return

        frame_path = QPainterPath()
        frame_path.addRoundedRect(QRectF(self.rect()), float(self._corner_radius), float(self._corner_radius))
        frame_region = QRegion(frame_path.toFillPolygon().toPolygon())
        self.setMask(frame_region)

        video_radius = max(0, self._corner_radius - self._border_width)
        if video_radius == 0:
            self._video_widget.clearMask()
            return

        video_path = QPainterPath()
        video_path.addRoundedRect(
            QRectF(self._video_widget.rect()), float(video_radius), float(video_radius)
        )
        video_region = QRegion(video_path.toFillPolygon().toPolygon())
        self._video_widget.setMask(video_region)


class PreviewWindow(QWidget):
    """Frameless preview surface that reuses the media controller API."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        require_multimedia()
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._shadow_padding = 12
        self._corner_radius = PREVIEW_WINDOW_CORNER_RADIUS
        default_height = max(1, int(PREVIEW_WINDOW_DEFAULT_WIDTH * 9 / 16))
        self._content_size = QSize(PREVIEW_WINDOW_DEFAULT_WIDTH, default_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            self._shadow_padding,
            self._shadow_padding,
            self._shadow_padding,
            self._shadow_padding,
        )
        layout.setSpacing(0)

        self._frame = _PreviewFrame(self._corner_radius, self)
        layout.addWidget(self._frame)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(48.0)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 120))
        self._frame.setGraphicsEffect(shadow)

        self._apply_content_size(
            self._content_size.width(),
            self._content_size.height(),
        )

        self._media = MediaController(self)
        self._media.set_video_output(self._frame.video_widget())
        self._media.set_muted(PREVIEW_WINDOW_MUTED)

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._do_close)
        self.hide()

    def show_preview(self, source: Path | str, at: Optional[QRect | QPoint] = None) -> None:
        """Display *source* near *at* and start playback immediately."""

        path = Path(source)
        self._close_timer.stop()
        self._media.stop()
        self._media.load(path)

        if isinstance(at, QRect):
            width = max(PREVIEW_WINDOW_DEFAULT_WIDTH, at.width())
            height = max(int(width * 9 / 16), at.height())
            width = max(width, int(height * 16 / 9))
            self._apply_content_size(width, height)
            center = at.center()
            origin = QPoint(center.x() - self.width() // 2, center.y() - self.height() // 2)
            origin = self._clamp_to_screen(origin)
            self.move(origin)
        else:
            width = PREVIEW_WINDOW_DEFAULT_WIDTH
            height = max(1, int(width * 9 / 16))
            self._apply_content_size(width, height)
            if isinstance(at, QPoint):
                origin = self._clamp_to_screen(at)
                self.move(origin)

        self.show()
        self.raise_()
        self._media.play()

    def close_preview(self, delayed: bool = True) -> None:
        """Hide the preview window, optionally with a delay."""

        if delayed:
            self._close_timer.start(PREVIEW_WINDOW_CLOSE_DELAY_MS)
        else:
            self._do_close()

    def _do_close(self) -> None:
        self._close_timer.stop()
        self._media.stop()
        self.hide()

    def _clamp_to_screen(self, origin: QPoint) -> QPoint:
        screen = self.screen()
        if screen is None:
            return origin
        area = screen.availableGeometry()
        min_x = area.x()
        min_y = area.y()
        max_x = area.x() + max(0, area.width() - self.width())
        max_y = area.y() + max(0, area.height() - self.height())
        return QPoint(
            max(min_x, min(origin.x(), max_x)),
            max(min_y, min(origin.y(), max_y)),
        )

    def _apply_content_size(self, content_width: int, content_height: int) -> None:
        content_width = max(1, content_width)
        content_height = max(1, content_height)
        self._content_size = QSize(content_width, content_height)
        self._frame.setFixedSize(self._content_size)
        total_width = self._content_size.width() + 2 * self._shadow_padding
        total_height = self._content_size.height() + 2 * self._shadow_padding
        if self.size() != QSize(total_width, total_height):
            self.resize(total_width, total_height)
        self._frame.set_corner_radius(self._corner_radius)
