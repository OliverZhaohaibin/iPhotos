"""Widget combining the video surface and floating playback controls."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QSize,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QPalette, QResizeEvent
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

try:  # pragma: no cover - optional Qt module
    from PySide6.QtMultimediaWidgets import QVideoWidget
except (ModuleNotFoundError, ImportError):  # pragma: no cover - handled by main window guard
    QVideoWidget = None  # type: ignore[assignment, misc]

from ....config import (
    PLAYER_CONTROLS_HIDE_DELAY_MS,
    PLAYER_FADE_IN_MS,
    PLAYER_FADE_OUT_MS,
)
from .player_bar import PlayerBar
from ..palette import VIEWER_SURFACE_COLOR_HEX, viewer_surface_color


class VideoArea(QWidget):
    """Present a video surface with auto-hiding playback controls."""

    mouseActive = Signal()
    controlsVisibleChanged = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)

        if QVideoWidget is None:
            raise RuntimeError("PySide6.QtMultimediaWidgets is required for video playback.")

        # --- Video Widget Setup -------------------------------------------------
        self._video_widget = QVideoWidget(self)
        self._video_widget.setMouseTracking(True)
        self._video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._video_widget.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        # Adopt the shared viewer surface colour on the container so any padding
        # around the video matches the still-photo viewer and the surrounding UI.
        surface_color = viewer_surface_color(self) or VIEWER_SURFACE_COLOR_HEX
        self.setStyleSheet(
            f"background: {surface_color}; border: none;"
        )

        # ``QVideoWidget`` composites decoded frames on a renderer-specific
        # surface.  Using a pure black background prevents the rendering backend
        # from blending UI colours into the video signal, which would otherwise
        # lift shadows or desaturate HDR footage.  Explicit palette and stylesheet
        # updates ensure the black surface is respected on all platforms.
        self._video_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._video_widget.setAutoFillBackground(True)
        palette = self._video_widget.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("black"))
        self._video_widget.setPalette(palette)
        self._video_widget.setStyleSheet("background: black; border: none;")
        # --- End Video Widget Setup --------------------------------------------

        # Cache the active video dimensions so the widget can be centred with a
        # matching aspect ratio.  ``None`` denotes that the surface should fill
        # the entire container, mirroring the legacy behaviour when no metadata
        # is available.
        self._video_size: Optional[QSize] = None

        self._overlay_margin = 48
        self._player_bar = PlayerBar(self)
        self._player_bar.hide()
        self._player_bar.setMouseTracking(True)

        self._controls_visible = False
        self._target_opacity = 0.0
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
        self._hide_timer.timeout.connect(self.hide_controls)

        self._install_activity_filters()
        self._wire_player_bar()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def video_item(self) -> QVideoWidget:
        """Return the embedded :class:`QVideoWidget` used for media playback.

        The method name is kept for backwards compatibility with older call
        sites that expect a ``video_item`` attribute while the underlying
        implementation has switched back to ``QVideoWidget`` to restore proper
        HDR rendering behaviour.
        """

        return self._video_widget

    @property
    def player_bar(self) -> PlayerBar:
        """Return the floating :class:`PlayerBar`."""

        return self._player_bar

    def set_video_size(self, width: Optional[int], height: Optional[int]) -> None:
        """Record the active clip dimensions and relayout the video surface.

        ``width`` and ``height`` are expected to be pixel dimensions for the
        decoded video stream.  Passing ``None`` for either value clears any
        cached size so the video widget falls back to filling the container.  A
        no-op occurs when the normalised dimensions have not changed, avoiding
        redundant layout work while scrubbing through clips that share the same
        resolution.
        """

        normalized: Optional[QSize]
        if (
            width is None
            or height is None
            or int(width) <= 0
            or int(height) <= 0
        ):
            normalized = None
        else:
            normalized = QSize(int(width), int(height))

        if self._video_size == normalized:
            return

        self._video_size = normalized
        self._update_video_geometry()

    def show_controls(self, *, animate: bool = True) -> None:
        """Reveal the playback controls and restart the hide timer."""

        if not self._controls_enabled:
            return
        self._hide_timer.stop()
        if not self._controls_visible:
            self._controls_visible = True
            self.controlsVisibleChanged.emit(True)

        if not self._player_bar.isVisible():
            self._player_bar.show()
            self._update_bar_geometry()

        duration = PLAYER_FADE_IN_MS if animate else 0
        self._animate_to(1.0, duration)
        self._restart_hide_timer()

    def hide_controls(self, *, animate: bool = True) -> None:
        """Fade the playback controls out."""

        if not self._controls_visible and self._current_opacity() <= 0.0:
            return
        self._hide_timer.stop()
        if self._controls_visible:
            self._controls_visible = False
            self.controlsVisibleChanged.emit(False)

        duration = PLAYER_FADE_OUT_MS if animate else 0
        self._animate_to(0.0, duration)

    def note_activity(self) -> None:
        """Treat external events as user activity to keep controls visible."""

        if not self._controls_enabled:
            return
        if self._controls_visible:
            self._restart_hide_timer()
        else:
            self.show_controls()

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event: QResizeEvent) -> None:  # pragma: no cover - GUI behaviour
        """Manually layout child widgets."""

        super().resizeEvent(event)
        self._update_video_geometry()
        self._update_bar_geometry()

    def changeEvent(self, event: QEvent) -> None:  # pragma: no cover - palette updates
        """Refresh palette-derived styling when the widget theme changes."""

        if event.type() == QEvent.Type.PaletteChange:
            surface_color = viewer_surface_color(self) or VIEWER_SURFACE_COLOR_HEX
            self.setStyleSheet(f"background: {surface_color}; border: none;")
        super().changeEvent(event)

    def enterEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().enterEvent(event)
        self.show_controls()

    def leaveEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().leaveEvent(event)
        if not self._player_bar.underMouse():
            self.hide_controls()

    def mouseMoveEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        self._on_mouse_activity()
        super().mouseMoveEvent(event)

    def hideEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().hideEvent(event)
        self.hide_controls(animate=False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # pragma: no cover - GUI behaviour
        if watched is self._video_widget:
            if event.type() in {
                QEvent.Type.MouseMove,
                QEvent.Type.HoverMove,
                QEvent.Type.HoverEnter,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.Wheel,
            }:
                self._on_mouse_activity()
            if event.type() == QEvent.Type.Enter:
                self.show_controls()
            if event.type() == QEvent.Type.Leave and not self._player_bar.underMouse():
                self.hide_controls()
        elif watched is self._player_bar:
            if event.type() in {
                QEvent.Type.MouseMove,
                QEvent.Type.HoverMove,
                QEvent.Type.HoverEnter,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.Wheel,
            }:
                self._on_mouse_activity()
            if event.type() == QEvent.Type.Leave:
                cursor_pos = QCursor.pos()
                if not self.rect().contains(self.mapFromGlobal(cursor_pos)):
                    self.hide_controls()

        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _install_activity_filters(self) -> None:
        self._video_widget.installEventFilter(self)
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
        else:
            self.show_controls()

    def _restart_hide_timer(self) -> None:
        if self.player_bar.is_scrubbing():
            self._hide_timer.stop()
        elif self._controls_visible:
            self._hide_timer.start(PLAYER_CONTROLS_HIDE_DELAY_MS)

    def _animate_to(self, value: float, duration: int) -> None:
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._current_opacity())
        self._fade_anim.setEndValue(value)
        self._fade_anim.setDuration(max(0, duration))
        self._target_opacity = value
        if duration > 0:
            self._fade_anim.start()
        else:
            self._set_opacity(value)
            self._on_fade_finished()

    def _current_opacity(self) -> float:
        effect = self._player_bar.graphicsEffect()
        return effect.opacity() if isinstance(effect, QGraphicsOpacityEffect) else 1.0

    def _set_opacity(self, value: float) -> None:
        effect = self._player_bar.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(max(0.0, min(1.0, value)))

    def _on_fade_finished(self) -> None:
        if self._target_opacity <= 0.0:
            self._player_bar.hide()

    def _update_bar_geometry(self) -> None:
        if not self.isVisible():
            return
        rect = self.rect()
        available_width = max(0, rect.width() - (2 * self._overlay_margin))
        bar_hint = self._player_bar.sizeHint()
        bar_width = min(bar_hint.width(), available_width)
        bar_height = bar_hint.height()
        x = (rect.width() - bar_width) // 2
        y = rect.height() - bar_height - self._overlay_margin
        if y < self._overlay_margin:
            y = max(0, rect.height() - bar_height)
        self._player_bar.setGeometry(x, y, bar_width, bar_height)
        self._player_bar.raise_()

    def _update_video_geometry(self) -> None:
        """Size and centre the ``QVideoWidget`` according to the clip aspect."""

        rect = self.rect()
        if rect.isNull() or rect.width() <= 0 or rect.height() <= 0:
            return

        if not self._video_size or self._video_size.isEmpty():
            self._video_widget.setGeometry(rect)
            return

        target = self._video_size.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
        width = target.width()
        height = target.height()
        if width <= 0 or height <= 0:
            self._video_widget.setGeometry(rect)
            return

        x = rect.x() + (rect.width() - width) // 2
        y = rect.y() + (rect.height() - height) // 2
        self._video_widget.setGeometry(x, y, width, height)

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
            self._player_bar.hide()

