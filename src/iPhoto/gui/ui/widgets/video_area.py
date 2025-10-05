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
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QStackedLayout,
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


class VideoArea(QWidget):
    """Present a video widget with an auto-hiding playback bar overlay."""

    mouseActive = Signal()
    controlsVisibleChanged = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)

        self._video_widget = QVideoWidget(self)
        self._video_widget.setMouseTracking(True)

        self._player_bar = PlayerBar(self)
        self._player_bar.hide()

        self._overlay = QWidget(self)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._overlay.setStyleSheet("background: transparent;")
        self._overlay.setMouseTracking(True)

        overlay_layout = QVBoxLayout(self._overlay)
        overlay_layout.setContentsMargins(24, 24, 24, 24)
        overlay_layout.setSpacing(0)
        overlay_layout.addStretch(1)
        overlay_layout.addWidget(
            self._player_bar,
            alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        )

        layout = QStackedLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        layout.addWidget(self._video_widget)
        layout.addWidget(self._overlay)
        layout.setCurrentWidget(self._video_widget)
        self._overlay.raise_()

        self._controls_visible = False
        self._target_opacity = 0.0

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

        self._install_activity_filters()
        self._wire_player_bar()

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

        self._hide_timer.stop()
        if not self._controls_visible:
            self._controls_visible = True
            self.controlsVisibleChanged.emit(True)
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

        if self._controls_visible:
            self._restart_hide_timer()
        else:
            self.show_controls()

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def enterEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().enterEvent(event)
        self.show_controls()

    def leaveEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().leaveEvent(event)
        self.hide_controls()

    def mouseMoveEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        self._on_mouse_activity()
        super().mouseMoveEvent(event)

    def hideEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().hideEvent(event)
        self._hide_timer.stop()
        self._fade_anim.stop()
        self._set_opacity(0.0)
        self._player_bar.hide()
        was_visible = self._controls_visible
        self._controls_visible = False
        if was_visible:
            self.controlsVisibleChanged.emit(False)

    def showEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().showEvent(event)
        self._overlay.raise_()

    def eventFilter(self, watched: QObject, event: QEvent):  # pragma: no cover - GUI behaviour
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
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _install_activity_filters(self) -> None:
        for widget in (self, self._overlay, self._player_bar, self._video_widget):
            widget.setMouseTracking(True)
            if widget is not self:
                widget.installEventFilter(self)

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
        self.mouseActive.emit()
        if self._controls_visible:
            self._restart_hide_timer()
        else:
            self.show_controls()

    def _restart_hide_timer(self) -> None:
        if not self._controls_visible:
            return
        self._hide_timer.start(PLAYER_CONTROLS_HIDE_DELAY_MS)

    def _on_hide_timeout(self) -> None:
        self.hide_controls()

    def _ensure_bar_visible(self) -> None:
        self._overlay.raise_()
        if not self._player_bar.isVisible():
            self._player_bar.show()

    def _animate_to(self, value: float, duration: int) -> None:
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._current_opacity())
        self._fade_anim.setEndValue(value)
        self._fade_anim.setDuration(max(0, duration))
        self._target_opacity = value
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

