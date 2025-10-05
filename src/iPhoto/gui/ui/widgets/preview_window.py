"""Lightweight floating window that previews a video asset."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, QSizeF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from ....config import (
    PREVIEW_WINDOW_CLOSE_DELAY_MS,
    PREVIEW_WINDOW_CORNER_RADIUS,
    PREVIEW_WINDOW_DEFAULT_WIDTH,
    PREVIEW_WINDOW_MUTED,
)
from ..media import MediaController, require_multimedia

if importlib.util.find_spec("PySide6.QtMultimediaWidgets") is not None:
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
else:  # pragma: no cover - requires optional Qt module
    QGraphicsVideoItem = None  # type: ignore[assignment]

if importlib.util.find_spec("PySide6.QtMultimedia") is not None:
    from PySide6.QtMultimedia import QMediaPlayer
else:  # pragma: no cover - requires optional Qt module
    QMediaPlayer = None  # type: ignore[assignment]


class _RoundedVideoItem(QGraphicsVideoItem):
    """Graphics video item that clips playback to a rounded rectangle."""

    def __init__(self, corner_radius: int) -> None:
        if QGraphicsVideoItem is None:  # pragma: no cover - optional Qt module
            raise RuntimeError(
                "PySide6.QtMultimediaWidgets is unavailable; install PySide6 with "
                "QtMultimediaWidgets support to enable video previews."
            )
        super().__init__()
        self._corner_radius = float(max(0, corner_radius))
        self.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatioByExpanding)

    def set_corner_radius(self, corner_radius: int) -> None:
        radius = float(max(0, corner_radius))
        if self._corner_radius == radius:
            return
        self._corner_radius = radius
        self.update()

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type: ignore[override]
        if self._corner_radius > 0.0:
            rect = self.boundingRect()
            radius = min(self._corner_radius, min(rect.width(), rect.height()) / 2.0)
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            painter.setClipPath(path)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        super().paint(painter, option, widget)


class _VideoView(QGraphicsView):
    """Hosts the rounded video item within a scene."""

    def __init__(
        self,
        on_resize: Callable[[], None],
        corner_radius: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._on_resize = on_resize
        self._scene = QGraphicsScene(self)
        self._video_item = _RoundedVideoItem(corner_radius)
        self._scene.addItem(self._video_item)
        self.setScene(self._scene)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

    def video_item(self) -> _RoundedVideoItem:
        return self._video_item

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_video_geometry()
        self._on_resize()

    def _update_video_geometry(self) -> None:
        viewport_size = self.viewport().size()
        rect = QRectF(
            0.0,
            0.0,
            float(max(0, viewport_size.width())),
            float(max(0, viewport_size.height())),
        )
        self._scene.setSceneRect(rect)
        self._video_item.setSize(rect.size())
        center = rect.center()
        self._video_item.setPos(center - self._video_item.boundingRect().center())


class _PreviewFrame(QWidget):
    """Draws rounded chrome around the embedded video widget."""

    def __init__(self, corner_radius: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._corner_radius = max(0, corner_radius)
        self._border_width = 0
        self._background = QColor(18, 18, 22)
        self._border = QColor(255, 255, 255, 28)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.setSpacing(0)

        self._poster_label = QLabel(self)
        self._poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._poster_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._poster_label.setStyleSheet("background: transparent;")
        self._poster_label.setVisible(False)

        self._video_view = _VideoView(self._update_masks, corner_radius, self)

        self._stack.addWidget(self._poster_label)
        self._stack.addWidget(self._video_view)

        self._poster_pixmap: Optional[QPixmap] = None

        self._update_masks()

    def video_item(self) -> _RoundedVideoItem:
        return self._video_view.video_item()

    def set_poster(self, poster: Optional[QPixmap]) -> None:
        if poster is None or poster.isNull():
            self._poster_pixmap = None
            self._poster_label.clear()
            self._poster_label.setVisible(False)
            self._stack.setCurrentWidget(self._video_view)
            return
        self._poster_pixmap = poster
        self._poster_label.setVisible(True)
        self._update_poster_display()
        self._stack.setCurrentWidget(self._poster_label)

    def clear_poster(self) -> None:
        self._poster_pixmap = None
        self._poster_label.clear()
        self._poster_label.setVisible(False)
        self._stack.setCurrentWidget(self._video_view)

    def show_video(self) -> None:
        self._stack.setCurrentWidget(self._video_view)
        self._poster_label.setVisible(False)

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
        self._update_poster_display()

    def _update_masks(self) -> None:
        video_radius = max(0, self._corner_radius)
        self._video_view.video_item().set_corner_radius(video_radius)
        self.update()

    def _update_poster_display(self) -> None:
        if self._poster_pixmap is None or self._poster_pixmap.isNull():
            return
        target_size = self._poster_label.size()
        if not target_size.isValid() or target_size.isEmpty():
            self._poster_label.setPixmap(self._poster_pixmap)
            return
        scaled = self._poster_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._poster_label.setPixmap(scaled)


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
        self._media.set_video_output(self._frame.video_item())
        self._media.set_muted(PREVIEW_WINDOW_MUTED)
        self._media.readyToPlay.connect(self._handle_media_ready)
        self._media.playbackStateChanged.connect(self._handle_playback_state_changed)

        self._poster_active = False
        self._current_source: Optional[Path] = None

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._do_close)
        self.hide()

    def show_preview(
        self,
        source: Path | str,
        at: Optional[QRect | QPoint] = None,
        *,
        poster_frame: Optional[QPixmap] = None,
    ) -> None:
        """Display *source* near *at* and show ``poster_frame`` until playback is ready."""

        path = Path(source)
        poster = poster_frame if poster_frame is not None and not poster_frame.isNull() else None
        self._poster_active = poster is not None
        self._current_source = path
        self._frame.set_poster(poster)
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

    def close_preview(self, delayed: bool = True) -> None:
        """Hide the preview window, optionally with a delay."""

        if delayed:
            self._close_timer.start(PREVIEW_WINDOW_CLOSE_DELAY_MS)
        else:
            self._do_close()

    def _do_close(self) -> None:
        self._close_timer.stop()
        self._media.stop()
        self._poster_active = False
        self._current_source = None
        self._frame.clear_poster()
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

    def _handle_media_ready(self) -> None:
        if self._current_source is None:
            return
        current = self._media.current_source()
        if current is None or current != self._current_source:
            return
        self._media.play()

    def _handle_playback_state_changed(self, state: object) -> None:
        if not self._poster_active:
            return
        current = self._media.current_source()
        if current is None or current != self._current_source:
            return

        is_playing = False
        if QMediaPlayer is not None:
            try:
                playback_state = QMediaPlayer.PlaybackState(state)  # type: ignore[arg-type]
            except Exception:  # pragma: no cover - Qt enum conversion can fail in mocks
                playback_state = None
            else:
                is_playing = playback_state == QMediaPlayer.PlaybackState.PlayingState

        if not is_playing:
            if getattr(state, "name", None) == "PlayingState":
                is_playing = True

        if not is_playing:
            return

        self._poster_active = False
        self._frame.show_video()
