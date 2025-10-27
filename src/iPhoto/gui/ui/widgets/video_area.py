"""Widget combining the video surface and floating playback controls."""

from __future__ import annotations

from typing import Optional, cast

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QPointF,
    QRectF,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QMouseEvent, QPainter, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

try:  # pragma: no cover - optional Qt module
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
except (ModuleNotFoundError, ImportError):  # pragma: no cover - handled by main window guard
    QGraphicsVideoItem = None  # type: ignore[assignment, misc]

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
    fullscreenExitRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)

        if QGraphicsVideoItem is None:
            raise RuntimeError("PySide6.QtMultimediaWidgets is required for video playback.")

        # --- Graphics View Setup ---
        # The dedicated black rectangle lives directly behind the video surface and is kept in
        # sync with the rendered frame geometry.  This avoids altering the neutral UI chrome
        # colour while ensuring the darkest pixels inside the video appear truly black.
        self._black_backing: QGraphicsRectItem = QGraphicsRectItem()
        self._black_backing.setBrush(Qt.black)
        self._black_backing.setPen(Qt.NoPen)
        self._black_backing.setZValue(0)

        self._video_item = QGraphicsVideoItem()
        self._video_item.setZValue(1)
        self._video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._video_item.nativeSizeChanged.connect(self._update_black_backing_geometry)

        self._scene = QGraphicsScene(self)
        self._scene.addItem(self._black_backing)
        self._scene.addItem(self._video_item)

        self._video_view = QGraphicsView(self._scene, self)
        self._video_view.setFrameShape(QFrame.Shape.NoFrame)
        self._video_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._video_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._video_view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Accept focus so keyboard navigation targets the video viewport without requiring the user
        # to click a non-interactive chrome element first.
        self._video_view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFocusProxy(self._video_view)
        # Match the photo viewer's light-toned surface so letterboxed video frames sit
        # on the same neutral backdrop.  Using the shared palette value keeps the
        # photo and video experiences visually consistent while avoiding harsh
        # contrast against the surrounding chrome.
        # Mirror the palette-driven detail background so letterboxed frames do not
        # sit on a subtly different tone compared to the surrounding widgets.
        surface_color = viewer_surface_color(self)
        self._default_surface_color = surface_color
        # Style both the graphics view and its viewport so any revealed margins match the
        # surrounding detail panel while the application is in its standard chrome mode.
        self._video_view.setStyleSheet("background: transparent; border: none;")
        self._video_view.viewport().setStyleSheet(
            f"background-color: {surface_color}; border: none;"
        )
        self.setStyleSheet(f"background-color: {surface_color};")
        self._scene.setBackgroundBrush(QColor(surface_color))
        # --- End Graphics View Setup ---

        self._overlay_margin = 48
        self._player_bar = PlayerBar(self)
        self._player_bar.hide()
        self._player_bar.setMouseTracking(True)

        self._controls_visible = False
        self._target_opacity = 0.0
        self._host_widget: QWidget | None = self._video_view
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
        self._hide_timer.timeout.connect(self.hide_controls)

        self._install_activity_filters()
        self._wire_player_bar()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def video_item(self) -> QGraphicsVideoItem:
        """Return the embedded :class:`QGraphicsVideoItem` for media output."""

        return self._video_item

    @property
    def player_bar(self) -> PlayerBar:
        """Return the floating :class:`PlayerBar`."""

        return self._player_bar

    def set_immersive_background(self, immersive: bool) -> None:
        """Switch to a pure black canvas when immersive full screen mode is active."""

        colour = "#000000" if immersive else self._default_surface_color
        stylesheet = f"background-color: {colour}; border: none;"
        self.setStyleSheet(stylesheet)
        self._video_view.setStyleSheet("background: transparent; border: none;")
        self._video_view.viewport().setStyleSheet(stylesheet)
        self._scene.setBackgroundBrush(QColor(colour))

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
        rect = self.rect()
        self._video_view.setGeometry(rect)
        self._scene.setSceneRect(QRectF(rect))
        self._video_item.setSize(self._scene.sceneRect().size())
        self._video_item.setPos(QPointF())
        self._update_bar_geometry()
        self._update_black_backing_geometry()

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

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # pragma: no cover - GUI behaviour
        """Emit a dedicated signal so the window can exit immersive full screen."""

        if event.button() == Qt.MouseButton.LeftButton:
            self.fullscreenExitRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def hideEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().hideEvent(event)
        self.hide_controls(animate=False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # pragma: no cover - GUI behaviour
        if event.type() in {
            QEvent.Type.MouseMove,
            QEvent.Type.HoverMove,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.Wheel,
        }:
            self._on_mouse_activity()

        if watched is self._player_bar and event.type() == QEvent.Type.Leave:
            cursor_pos = QCursor.pos()
            if not self.rect().contains(self.mapFromGlobal(cursor_pos)):
                self.hide_controls()

        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _install_activity_filters(self) -> None:
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

    def video_view(self) -> QGraphicsView:
        """Return the graphics view hosting the video surface."""

        # Exposing the graphics view allows higher-level widgets to install
        # shortcut filters directly on the focus target instead of reaching into
        # private attributes.  The method keeps the detail view wiring explicit
        # while still encapsulating the scene graph setup within ``VideoArea``.
        return self._video_view

    def video_viewport(self) -> QWidget:
        """Return the viewport widget that accepts keyboard focus."""

        # Keyboard shortcuts are intercepted at the viewport level so the main
        # window can consume navigation keys before Qt treats them as focus
        # traversal requests.  Providing the widget through a helper keeps the
        # shortcut configuration readable from the controller layer.
        return self._video_view.viewport()

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

    def _update_black_backing_geometry(self) -> None:
        """Keep the black backing rectangle aligned with the rendered video frame."""

        video_native_size = self._video_item.nativeSize()
        if video_native_size.isEmpty():
            # Collapse the rectangle when no video is loaded so the neutral UI background stays
            # visible.  This avoids showing a stray black patch before playback starts.
            self._black_backing.setRect(QRectF())
            return

        item_size = self._video_item.size()
        if item_size.isEmpty():
            # Skip updates until the view establishes a non-zero drawing area.  Attempting to
            # scale into a zero-sized surface would lead to divisions by zero inside Qt.
            self._black_backing.setRect(QRectF())
            return

        # Determine the aspect-ratio preserving rectangle that Qt will use to present the video.
        # We start with the native pixel size and scale it into the current video item bounds while
        # maintaining the user's configured aspect mode.
        scaled_size = video_native_size.scaled(item_size, Qt.AspectRatioMode.KeepAspectRatio)
        scaled_rect = QRectF(QPointF(), scaled_size)

        # Position the scaled rectangle so it is centred inside the video item, matching the
        # placement of the actual media frame.
        item_bounds = QRectF(QPointF(), item_size)
        scaled_rect.moveCenter(item_bounds.center())

        self._black_backing.setPos(self._video_item.pos())
        self._black_backing.setRect(scaled_rect)

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

    def controls_enabled(self) -> bool:
        """Return ``True`` when the playback controls are currently enabled."""

        return self._controls_enabled

