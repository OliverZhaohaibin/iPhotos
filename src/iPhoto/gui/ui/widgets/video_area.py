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
from ..palette import viewer_surface_color


class VideoArea(QWidget):
    """Present a video surface with auto-hiding playback controls."""

    mouseActive = Signal()
    controlsVisibleChanged = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoArea")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)

        if QVideoWidget is None:
            raise RuntimeError("PySide6.QtMultimediaWidgets is required for video playback.")

        # --- Video Widget Setup -------------------------------------------------
        self._video_widget = QVideoWidget(self)
        self._video_widget.setMouseTracking(True)
        self._video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._video_widget.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # The parent ``VideoArea`` fills the gaps that appear when the video is
        # letterboxed or pillarboxed, so it adopts the shared viewer surface
        # colour.  The tone previously matched a constant fallback, but now the
        # widget queries :func:`viewer_surface_color` so the fill color updates
        # alongside theme or palette changes while keeping full parity with the
        # photo viewer background.  Applying the stylesheet through an ID
        # selector confines the background colour to the container only and
        # prevents child widgets such as ``PlayerBar`` from inheriting the solid
        # fill that would otherwise obscure their translucent design.
        surface_color = viewer_surface_color(self)
        self.setStyleSheet(
            f"VideoArea#videoArea {{ background: {surface_color}; border: none; }}"
        )

        # ``QVideoWidget`` internally composites frames on a platform-specific
        # surface.  Setting that surface to pure black avoids blending artefacts
        # that would otherwise brighten shadows or wash out tone-mapped HDR
        # content.  The widget therefore keeps a black background even though its
        # container is bright.
        self._video_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._video_widget.setAutoFillBackground(True)
        palette = self._video_widget.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("black"))
        self._video_widget.setPalette(palette)
        self._video_widget.setStyleSheet("background: black; border: none;")
        # --- End Video Widget Setup --------------------------------------------

        # Track the logical video resolution reported by the currently playing
        # asset.  ``None`` indicates that either no clip is active or that the
        # metadata did not include dimensions.  The value is used to manually
        # size the ``QVideoWidget`` so that the black surface only covers the
        # actual video content while the surrounding padding inherits the
        # theme-aware container background.
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

    def set_video_size(self, width: Optional[int], height: Optional[int]) -> None:
        """Update the logical video resolution driving the surface layout."""

        if (
            width is None
            or height is None
            or int(width) <= 0
            or int(height) <= 0
        ):
            target_size: Optional[QSize] = None
        else:
            target_size = QSize(int(width), int(height))

        if self._video_size == target_size:
            return

        self._video_size = target_size
        # Immediately adjust the geometry so transitions between assets do not
        # wait for a resize event (which may never arrive if the widget keeps
        # the same outer dimensions).
        self._update_video_geometry()

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event: QResizeEvent) -> None:  # pragma: no cover - GUI behaviour
        """Manually layout child widgets."""

        super().resizeEvent(event)
        self._update_video_geometry()
        self._update_bar_geometry()

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

        # ``QVideoWidget.geometry()`` reflects the letterboxed video surface
        # after aspect-ratio corrections.  Aligning the playback controls with
        # that rectangle keeps them visually anchored to the visible clip rather
        # than stretching across the themed padding that surrounds narrow or
        # pillarboxed videos.
        video_rect = self._video_widget.geometry()

        # The geometry query occasionally races ahead of the layout pass that
        # gives ``QVideoWidget`` its final size.  During that short window the
        # widget reports a ``1x1`` placeholder rectangle which is technically
        # non-empty, so a simple ``isEmpty`` check is insufficient.  Falling back
        # to the full container whenever the surface is suspiciously small—or
        # when the clip dimensions have not yet been reported—keeps the controls
        # visible until the real geometry is ready.
        if (
            self._video_size is None
            or self._video_size.isEmpty()
            or video_rect.isEmpty()
            or video_rect.width() < 100
        ):
            video_rect = self.rect()

        # Derive the bar width from the usable video span while respecting the
        # overlay margin.  Clamping against ``sizeHint`` avoids overflowing into
        # the themed side gutters with very narrow clips.
        available_width = max(0, video_rect.width() - (2 * self._overlay_margin))
        bar_hint = self._player_bar.sizeHint()
        bar_width = min(bar_hint.width(), available_width)
        bar_height = bar_hint.height()

        # Keep the controls centred beneath the visible picture.  The geometry
        # is already expressed in parent-relative coordinates, so offset from
        # ``left`` instead of assuming the video starts at ``x == 0``.
        x = video_rect.left() + (video_rect.width() - bar_width) // 2

        # Position the bar just above the bottom margin.  If the available
        # height collapses (for instance with very small videos or aggressive UI
        # scaling) clamp the value so the controls never leave the video bounds.
        y = video_rect.bottom() - bar_height - self._overlay_margin
        if y < video_rect.top():
            y = video_rect.top()

        self._player_bar.setGeometry(x, y, bar_width, bar_height)
        self._player_bar.raise_()

    def _update_video_geometry(self) -> None:
        """Resize the video surface to match the reported clip dimensions."""

        rect = self.rect()
        if rect.isEmpty():
            self._video_widget.setGeometry(rect)
            return

        if self._video_size is None or self._video_size.isEmpty():
            # Without metadata fall back to the legacy behaviour where the
            # surface fills the entire area.  The ``QVideoWidget`` will apply its
            # own aspect ratio constraints, which recreates the previous
            # letterboxed look while still supporting theme-aligned padding.
            self._video_widget.setGeometry(rect)
            return

        available = rect.size()
        target = self._video_size.scaled(available, Qt.AspectRatioMode.KeepAspectRatio)
        x = (available.width() - target.width()) // 2
        y = (available.height() - target.height()) // 2
        self._video_widget.setGeometry(x, y, target.width(), target.height())

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

