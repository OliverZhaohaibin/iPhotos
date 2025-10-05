"""Lightweight floating window that previews a video asset."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

import sys

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
    QRegion,
    QResizeEvent,
    QShowEvent,
)
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
    QVideoWidget = None  # type: ignore[assignment]


class _ChromeWidget(QWidget):
    """Rounded chrome that paints the preview background and border."""

    def __init__(self, corner_radius: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._corner_radius = max(0, corner_radius)
        self._border_width = 1
        self._background = QColor(18, 18, 22, 255)
        self._border = QColor(255, 255, 255, 36)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

    def set_corner_radius(self, corner_radius: int) -> None:
        radius = max(0, corner_radius)
        if radius == self._corner_radius:
            return
        self._corner_radius = radius
        self._update_mask()
        self.update()

    def corner_radius(self) -> int:
        return self._corner_radius

    def border_width(self) -> int:
        return self._border_width

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_mask()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(self.rect())
        radius = float(self._corner_radius)
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._background)
        painter.drawPath(path)

        if self._border_width > 0 and self._border.alpha() > 0:
            pen = QPen(self._border)
            pen.setWidth(self._border_width)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

    def _update_mask(self) -> None:
        if self.width() <= 0 or self.height() <= 0:
            self.clearMask()
            return

        radius = float(self._corner_radius)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), radius, radius)
        region = QRegion(path.toFillPolygon().toPolygon())
        self.setMask(region)


class _RoundedVideoWidget(QVideoWidget):
    """``QVideoWidget`` that integrates with the preview chrome rounding."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        require_multimedia()
        if QVideoWidget is None:  # pragma: no cover - depends on optional Qt module
            raise RuntimeError(
                "PySide6.QtMultimediaWidgets is unavailable; install PySide6 with "
                "QtMultimediaWidgets support to enable video previews."
            )
        super().__init__(parent)
        self._corner_radius = 0

        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)

    def set_corner_radius(self, corner_radius: int) -> None:
        radius = max(0, corner_radius)
        if radius == self._corner_radius:
            return
        self._corner_radius = radius
        self._update_rounding()

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_rounding()

    def showEvent(self, event: QShowEvent) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._update_rounding()

    def _update_rounding(self) -> None:
        if self.width() <= 0 or self.height() <= 0 or self._corner_radius <= 0:
            _clear_native_rounding(self)
            self.clearMask()
            return

        radius = self._corner_radius
        if _apply_native_rounding(self, radius):
            self.clearMask()
            return

        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), float(radius), float(radius))
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))


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

        self._chrome = _ChromeWidget(self._corner_radius, self)
        self._chrome.setObjectName("previewChrome")
        self._chrome.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        chrome_layout = QVBoxLayout(self._chrome)
        border_width = self._chrome.border_width()
        chrome_layout.setContentsMargins(
            border_width,
            border_width,
            border_width,
            border_width,
        )
        chrome_layout.setSpacing(0)

        self._video_widget = _RoundedVideoWidget(self._chrome)
        self._video_widget.setObjectName("previewVideo")
        self._video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._video_widget.setStyleSheet("background: transparent; border: none;")
        chrome_layout.addWidget(self._video_widget)

        self._video_widget.set_corner_radius(max(0, self._corner_radius - border_width))

        layout.addWidget(self._chrome)

        self._shadow_effect = QGraphicsDropShadowEffect(self)
        self._shadow_effect.setBlurRadius(48.0)
        self._shadow_effect.setOffset(0, 12)
        self._shadow_effect.setColor(QColor(0, 0, 0, 120))
        self._chrome.setGraphicsEffect(self._shadow_effect)

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

    def showEvent(self, event: QShowEvent) -> None:  # type: ignore[override]
        super().showEvent(event)
        _apply_system_rounded_corners(self)

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

    def _apply_content_size(self, content_width: int, content_height: int) -> None:
        content_width = max(1, content_width)
        content_height = max(1, content_height)
        self._content_size = QSize(content_width, content_height)
        self._chrome.setFixedSize(self._content_size)
        total_width = self._content_size.width() + 2 * self._shadow_padding
        total_height = self._content_size.height() + 2 * self._shadow_padding
        if self.size() != QSize(total_width, total_height):
            self.resize(total_width, total_height)
        self._chrome.set_corner_radius(self._corner_radius)
        border_width = self._chrome.border_width()
        self._video_widget.set_corner_radius(max(0, self._corner_radius - border_width))


def _apply_system_rounded_corners(widget: QWidget) -> None:
    """Ask the native window manager for rounded corners when supported."""

    if sys.platform != "win32":  # Only Windows exposes the DWM rounding API we need.
        return

    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - optional dependency on Windows runtime
        return

    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_ROUND = 2

    try:
        hwnd = int(widget.winId())
    except Exception:  # pragma: no cover - winId may not be ready yet
        return

    if hwnd == 0:
        return

    preference = ctypes.c_uint(DWMWCP_ROUND)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )
    except Exception:  # pragma: no cover - failures shouldn't break previews
        return


def _apply_native_rounding(widget: QWidget, radius: int) -> bool:
    """Apply rounded clipping to *widget* using native APIs."""

    if sys.platform == "win32":
        try:
            import ctypes
        except Exception:  # pragma: no cover - optional Windows support
            return False
        hwnd = int(widget.winId()) if widget.winId() is not None else 0
        if hwnd:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
            dpr = widget.devicePixelRatioF()
            width = max(1, int(round(widget.width() * dpr)))
            height = max(1, int(round(widget.height() * dpr)))
            diameter = max(0, int(round(radius * 2 * dpr)))
            region = gdi32.CreateRoundRectRgn(0, 0, width, height, diameter, diameter)
            if region:
                user32.SetWindowRgn(hwnd, region, True)
                return True

    return False


def _clear_native_rounding(widget: QWidget) -> None:
    """Remove any native clipping previously applied by :func:`_apply_native_rounding`."""

    if sys.platform == "win32":
        try:
            import ctypes
        except Exception:  # pragma: no cover - optional Windows support
            pass
        else:
            hwnd = int(widget.winId()) if widget.winId() is not None else 0
            if hwnd:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                user32.SetWindowRgn(hwnd, 0, True)
