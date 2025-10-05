"""Lightweight floating window that previews a video asset."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainterPath, QRegion, QResizeEvent
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

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
    class QVideoWidget(QWidget):  # type: ignore[misc]
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - fallback
            raise RuntimeError(
                "PySide6.QtMultimediaWidgets is unavailable. Install PySide6 with "
                "QtMultimedia support to preview videos."
            )


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

        self._chrome = QWidget(self)
        self._chrome.setObjectName("previewChrome")
        self._chrome.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._chrome.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        chrome_layout = QVBoxLayout(self._chrome)
        chrome_layout.setContentsMargins(0, 0, 0, 0)

        self._video_widget = QVideoWidget(self._chrome)
        self._video_widget.setObjectName("previewVideo")
        self._video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        chrome_layout.addWidget(self._video_widget)

        layout.addWidget(self._chrome)

        self._shadow_effect = QGraphicsDropShadowEffect(self)
        self._shadow_effect.setBlurRadius(48.0)
        self._shadow_effect.setOffset(0, 12)
        self._shadow_effect.setColor(QColor(0, 0, 0, 120))
        self._chrome.setGraphicsEffect(self._shadow_effect)

        self._apply_palette()
        self._apply_content_size(
            self._content_size.width(),
            self._content_size.height(),
        )

        self._media = MediaController(self)
        self._media.set_video_output(self._video_widget)
        self._media.set_muted(PREVIEW_WINDOW_MUTED)

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._do_close)
        self.hide()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
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

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_masks()

    def _apply_palette(self) -> None:
        border_radius = self._corner_radius
        inner_radius = max(0, border_radius - 2)
        stylesheet = (
            "PreviewWindow #previewChrome {"
            " background-color: rgba(18, 18, 22, 220);"
            f" border-radius: {border_radius}px;"
            " border: 1px solid rgba(255, 255, 255, 36);"
            " }\n"
            "PreviewWindow #previewVideo {"
            f" border-radius: {inner_radius}px;"
            " background-color: black;"
            " }"
        )
        self.setStyleSheet(stylesheet)

    def _apply_content_size(self, content_width: int, content_height: int) -> None:
        content_width = max(1, content_width)
        content_height = max(1, content_height)
        self._content_size = QSize(content_width, content_height)
        self._chrome.setFixedSize(self._content_size)
        total_width = self._content_size.width() + 2 * self._shadow_padding
        total_height = self._content_size.height() + 2 * self._shadow_padding
        if self.size() != QSize(total_width, total_height):
            self.resize(total_width, total_height)
        self._update_masks()

    def _update_masks(self) -> None:
        if self._chrome.width() <= 0 or self._chrome.height() <= 0:
            self._chrome.clearMask()
            self._video_widget.clearMask()
            return

        radius = self._corner_radius
        chrome_path = QPainterPath()
        chrome_path.addRoundedRect(
            QRectF(self._chrome.rect()),
            float(radius),
            float(radius),
        )
        chrome_region = QRegion(chrome_path.toFillPolygon().toPolygon())
        self._chrome.setMask(chrome_region)

        if self._video_widget.width() <= 0 or self._video_widget.height() <= 0:
            self._video_widget.clearMask()
            return

        inner_radius = max(0, radius - 2)
        video_path = QPainterPath()
        video_path.addRoundedRect(
            QRectF(self._video_widget.rect()),
            float(inner_radius),
            float(inner_radius),
        )
        video_region = QRegion(video_path.toFillPolygon().toPolygon())
        self._video_widget.setMask(video_region)
