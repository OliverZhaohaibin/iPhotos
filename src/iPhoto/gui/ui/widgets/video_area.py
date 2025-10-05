"""Widget combining the video surface and floating playback controls."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QCursor, QMouseEvent
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

try:  # pragma: no cover - optional Qt module
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ModuleNotFoundError:  # pragma: no cover - handled by main window guard
    from PySide6.QtWidgets import QWidget as QVideoWidget  # type: ignore[misc]

from ....config import (
    PLAYER_CONTROLS_HIDE_DELAY_MS,
    PLAYER_FADE_IN_MS,
    PLAYER_FADE_OUT_MS,
)
from .player_bar import PlayerBar
from ..icons import load_icon


class VideoArea(QWidget):
    """Present a video widget with an auto-hiding playback bar overlay."""

    mouseActive = Signal()
    controlsVisibleChanged = Signal(bool)
    replayRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)

        self._video_widget = QVideoWidget(self)
        self._video_widget.setMouseTracking(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._video_widget)

        self._player_bar = PlayerBar()
        self._player_bar.hide()

        self._overlay = QWidget()
        self._overlay_margin = 48
        self._configure_overlay_window()

        overlay_layout = QVBoxLayout(self._overlay)
        overlay_layout.setContentsMargins(24, 24, 24, 24)
        overlay_layout.setSpacing(0)
        overlay_layout.addStretch(1)
        overlay_layout.addWidget(
            self._player_bar,
            alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        )

        self._controls_visible = False
        self._target_opacity = 0.0
        self._host_widget: QWidget | None = self._video_widget
        self._window_host: QWidget | None = None
        self._controls_enabled = True

        effect = QGraphicsOpacityEffect(self._player_bar)
        effect.setOpacity(0.0)
        self._player_bar.setGraphicsEffect(effect)

        self._fade_anim = QPropertyAnimation(effect, b"opacity", self)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_anim.finished.connect(self._on_fade_finished)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(PLAYER_CONTROLS_HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._on_hide_timeout)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh_controls)

        self._install_activity_filters()
        self._wire_player_bar()
        self.destroyed.connect(self._overlay.close)

        self._live_badge = QLabel(self)
        self._live_badge.hide()
        self._live_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        icon = load_icon("livephoto.svg", color="#cccccc")
        if not icon.isNull():
            self._live_badge.setPixmap(icon.pixmap(32, 32))
        self._live_badge.setFixedSize(32, 32)
        self._live_badge.move(12, 12)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def video_widget(self) -> QVideoWidget:
        """Return the embedded :class:`QVideoWidget`."""

        return self._video_widget

    @property
    def player_bar(self) -> PlayerBar:
        """Return the floating :class:`PlayerBar`."""

        return self._player_bar

    def show_controls(self, *, animate: bool = True) -> None:
        """Reveal the playback controls and restart the hide timer."""

        if not self._controls_enabled:
            return
        self._hide_timer.stop()
        if not self._controls_visible:
            self._controls_visible = True
            self.controlsVisibleChanged.emit(True)
        self._update_overlay_visibility()
        self._ensure_bar_visible()
        duration = PLAYER_FADE_IN_MS if animate else 0
        self._animate_to(1.0, duration)
        self._restart_hide_timer()

    def hide_controls(self, *, animate: bool = True) -> None:
        """Fade the playback controls out."""

        if not self._controls_visible and self._current_opacity() <= 0.0:
            return
        self._hide_timer.stop()
        state_changed = self._controls_visible
        self._controls_visible = False
        duration = PLAYER_FADE_OUT_MS if animate else 0
        self._animate_to(0.0, duration)
        if state_changed:
            self.controlsVisibleChanged.emit(False)

    def note_activity(self) -> None:
        """Treat external events as user activity to keep controls visible."""

        if not self._controls_enabled:
            return
        if self._controls_visible:
            self._restart_hide_timer()
            self._update_overlay_visibility()
        else:
            self.show_controls()

    def refresh_controls(self) -> None:
        """Realign the overlay to the video widget when visible."""

        if not self._controls_visible or not self._overlay.isVisible():
            return
        host = self._host_widget or self._video_widget
        if host is None or not host.isVisible():
            return
        rect = host.rect()
        if rect.isEmpty():
            return
        top_left = host.mapToGlobal(rect.topLeft())
        available_width = max(0, rect.width() - (2 * self._overlay_margin))
        if available_width <= 0 or rect.height() <= 0:
            return
        hint = self._overlay.sizeHint()
        overlay_width = min(hint.width(), available_width)
        overlay_height = hint.height()
        x = top_left.x() + (rect.width() - overlay_width) // 2
        y = top_left.y() + max(0, rect.height() - overlay_height - self._overlay_margin)
        self._overlay.setGeometry(x, y, overlay_width, overlay_height)
        self._overlay.raise_()

    def schedule_refresh(self, delay_ms: int = 0) -> None:
        """Queue a deferred refresh after layout or window changes."""

        if not self._controls_visible:
            return
        self._refresh_timer.stop()
        self._refresh_timer.start(max(0, delay_ms))

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def enterEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().enterEvent(event)
        self.show_controls()

    def leaveEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().leaveEvent(event)
        if not self._overlay.underMouse():
            self.hide_controls()

    def mouseMoveEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        self._on_mouse_activity()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # pragma: no cover - GUI behaviour
        if self._live_badge.isVisible() and event.button() == Qt.MouseButton.LeftButton:
            self.replayRequested.emit()
        super().mousePressEvent(event)

    def hideEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().hideEvent(event)
        self._hide_timer.stop()
        self._fade_anim.stop()
        self._set_opacity(0.0)
        self._player_bar.hide()
        self._overlay.hide()
        was_visible = self._controls_visible
        self._controls_visible = False
        if was_visible:
            self.controlsVisibleChanged.emit(False)

    def resizeEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().resizeEvent(event)
        self._live_badge.move(12, 12)

    def showEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().showEvent(event)
        self._bind_overlay_host()
        self._ensure_window_filter()
        self.schedule_refresh()

    def eventFilter(self, watched: QObject, event: QEvent):  # pragma: no cover - GUI behaviour
        if watched is self._video_widget:
            if event.type() in {
                QEvent.Type.Resize,
                QEvent.Type.Move,
                QEvent.Type.Show,
            }:
                self.schedule_refresh()
            if event.type() == QEvent.Type.Hide:
                self._overlay.hide()
            if event.type() in {
                QEvent.Type.MouseMove,
                QEvent.Type.HoverMove,
                QEvent.Type.HoverEnter,
                QEvent.Type.Enter,
                QEvent.Type.Wheel,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
            }:
                self._on_mouse_activity()
            if event.type() == QEvent.Type.Enter:
                self.show_controls()
            if event.type() == QEvent.Type.Leave and not self._overlay.underMouse():
                self.hide_controls()
        elif watched in {self._overlay, self._player_bar}:
            if event.type() in {
                QEvent.Type.MouseMove,
                QEvent.Type.HoverMove,
                QEvent.Type.HoverEnter,
                QEvent.Type.Enter,
                QEvent.Type.Wheel,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
            }:
                self._on_mouse_activity()
                if event.type() == QEvent.Type.Enter:
                    self.show_controls()
            if event.type() == QEvent.Type.Leave:
                cursor_pos = QCursor.pos()
                if not self.rect().contains(self.mapFromGlobal(cursor_pos)):
                    self.hide_controls()
        elif watched is self._window_host:
            if event.type() in {
                QEvent.Type.Move,
                QEvent.Type.Resize,
                QEvent.Type.Show,
            }:
                self.schedule_refresh()
            if event.type() == QEvent.Type.WindowStateChange:
                if self._window_host is not None and (
                    self._window_host.windowState() & Qt.WindowState.WindowMinimized
                ):
                    self._overlay.hide()
                else:
                    self.schedule_refresh()
            if event.type() == QEvent.Type.WindowActivate:
                if self._controls_visible:
                    self._update_overlay_visibility()
                    self.schedule_refresh()
            if event.type() in {
                QEvent.Type.Hide,
                QEvent.Type.WindowDeactivate,
            }:
                self._overlay.hide()
                self._hide_timer.stop()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _install_activity_filters(self) -> None:
        self._video_widget.installEventFilter(self)
        self._overlay.setMouseTracking(True)
        self._overlay.installEventFilter(self)
        self._player_bar.setMouseTracking(True)
        self._player_bar.installEventFilter(self)

    def _wire_player_bar(self) -> None:
        for signal in (
            self._player_bar.playPauseRequested,
            self._player_bar.scrubStarted,
            self._player_bar.scrubFinished,
        ):
            signal.connect(self._on_mouse_activity)
        self._player_bar.seekRequested.connect(lambda _value: self._on_mouse_activity())
        self._player_bar.volumeChanged.connect(lambda _value: self._on_mouse_activity())
        self._player_bar.muteToggled.connect(lambda _state: self._on_mouse_activity())

    def _on_mouse_activity(self) -> None:
        if not self._controls_enabled:
            return
        self.mouseActive.emit()
        if self._controls_visible:
            self._restart_hide_timer()
            self._update_overlay_visibility()
        else:
            self.show_controls()

    def _restart_hide_timer(self) -> None:
        if not self._controls_visible:
            return
        self._hide_timer.start(PLAYER_CONTROLS_HIDE_DELAY_MS)

    def _on_hide_timeout(self) -> None:
        self.hide_controls()

    def _ensure_bar_visible(self) -> None:
        self._ensure_overlay_parent()
        if not self._player_bar.isVisible():
            self._player_bar.show()
        if self._controls_visible and not self._overlay.isVisible():
            self._overlay.show()

    def _animate_to(self, value: float, duration: int) -> None:
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._current_opacity())
        self._fade_anim.setEndValue(value)
        self._fade_anim.setDuration(max(0, duration))
        self._target_opacity = value
        if value > 0.0:
            self._update_overlay_visibility()
        if duration <= 0:
            self._set_opacity(value)
            self._on_fade_finished()
        else:
            self._fade_anim.start()

    def _current_opacity(self) -> float:
        effect = self._player_bar.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            return effect.opacity()
        return 1.0

    def _set_opacity(self, value: float) -> None:
        effect = self._player_bar.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(max(0.0, min(1.0, value)))

    def _on_fade_finished(self) -> None:
        if self._target_opacity <= 0.0:
            self._player_bar.hide()
            self._overlay.hide()

    def _configure_overlay_window(self) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self._overlay.setWindowFlags(flags)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._overlay.hide()

    def _bind_overlay_host(self) -> None:
        host = self._video_widget
        if self._host_widget is host:
            return
        if self._host_widget is not None:
            self._host_widget.removeEventFilter(self)
        self._host_widget = host
        if self._host_widget is not None:
            self._host_widget.installEventFilter(self)

    def _ensure_window_filter(self) -> None:
        window = self.window()
        if window is self._window_host:
            return
        if self._window_host is not None:
            self._window_host.removeEventFilter(self)
        self._window_host = window
        if self._window_host is not None:
            self._window_host.installEventFilter(self)
            self._ensure_overlay_parent()

    def _ensure_overlay_parent(self) -> None:
        window = self.window()
        if window is None:
            return
        if self._overlay.parent() is window:
            return
        was_visible = self._overlay.isVisible()
        self._overlay.setParent(window)
        self._configure_overlay_window()
        if was_visible and self._controls_visible:
            self._overlay.show()
            self.schedule_refresh()

    def _update_overlay_visibility(self) -> None:
        if not self._controls_visible:
            self._overlay.hide()
            return
        window = self.window()
        if (
            window is None
            or not window.isVisible()
            or bool(window.windowState() & Qt.WindowState.WindowMinimized)
            or not window.isActiveWindow()
        ):
            self._overlay.hide()
            return
        self._ensure_overlay_parent()
        if not self._overlay.isVisible():
            self._overlay.show()
        self.schedule_refresh()

    # ------------------------------------------------------------------
    # Live Photo helpers
    # ------------------------------------------------------------------
    def set_controls_enabled(self, enabled: bool) -> None:
        """Enable or disable the floating playback controls."""

        if self._controls_enabled == enabled:
            return
        self._controls_enabled = enabled
        if not enabled:
            self.hide_controls(animate=False)
        else:
            self._controls_visible = False
            self._overlay.hide()
            self._player_bar.hide()

    def show_live_badge(self, visible: bool) -> None:
        """Toggle visibility of the Live Photo badge overlay."""

        self._live_badge.setVisible(visible)
        if visible:
            self._live_badge.raise_()

    def live_badge_visible(self) -> bool:
        """Return whether the Live Photo badge overlay is visible."""

        return self._live_badge.isVisible()
