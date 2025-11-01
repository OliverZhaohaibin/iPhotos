"""Global keyboard shortcut handling for the main window."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QObject, Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit

from ....config import VOLUME_SHORTCUT_STEP
from .playback_state_manager import PlayerState

if TYPE_CHECKING:  # pragma: no cover - import used for typing only
    from ..main_window import MainWindow
    from ..window_manager import FramelessWindowManager
    from .context_menu_controller import ContextMenuController
    from .main_controller import MainController
    from .navigation_controller import NavigationController
    from .view_controller_manager import ViewControllerManager


class ShortcutController(QObject):
    """Install a global event filter that routes keyboard shortcuts."""

    def __init__(
        self,
        window: "MainWindow",
        window_manager: FramelessWindowManager,
        main_controller: "MainController",
        view_manager: "ViewControllerManager",
        navigation: "NavigationController",
        context_menu: "ContextMenuController",
    ) -> None:
        super().__init__(window)
        self._window = window
        self._window_manager = window_manager
        self._controller = main_controller
        self._view_manager = view_manager
        self._navigation = navigation
        self._context_menu = context_menu

        self._app = QApplication.instance()
        if self._app is not None:
            self._app.installEventFilter(self)

    # ------------------------------------------------------------------
    # Lifecycle
    def shutdown(self) -> None:
        """Remove the global event filter during application shutdown."""

        if self._app is None:
            return
        self._app.removeEventFilter(self)
        self._app = None

    # ------------------------------------------------------------------
    # QObject API
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)

        key_event = cast(QKeyEvent, event)

        if self._handle_escape(key_event):
            return True

        focus_widget = self._focused_widget()
        if isinstance(focus_widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
            return super().eventFilter(watched, event)

        if self._handle_global_shortcut(key_event):
            return True

        if self._view_manager.is_detail_view_active():
            if self._handle_detail_view_shortcut(key_event):
                return True

        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Internal helpers
    def _focused_widget(self):
        return self._app.focusWidget() if self._app is not None else None

    def _handle_escape(self, event: QKeyEvent) -> bool:
        if event.key() != Qt.Key.Key_Escape:
            return False
        edit_controller = self._view_manager.edit_controller()
        if edit_controller.is_in_fullscreen():
            edit_controller.exit_fullscreen_preview()
            event.accept()
            return True

        if not self._window_manager.is_immersive_active():
            return False

        self._window_manager.exit_fullscreen()
        event.accept()
        return True

    def _handle_global_shortcut(self, event: QKeyEvent) -> bool:
        if self._window.ui.view_stack.currentWidget() is not self._window.ui.gallery_page:
            return False

        if self._navigation.is_recently_deleted_view():
            return False

        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        key = event.key()

        handled = False
        if modifiers == Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_D:
            handled = self._context_menu.delete_selection()
        elif modifiers == Qt.KeyboardModifier.MetaModifier and key in {
            Qt.Key.Key_Backspace,
            Qt.Key.Key_Delete,
        }:
            handled = self._context_menu.delete_selection()

        if handled:
            event.accept()
        return handled

    def _handle_detail_view_shortcut(self, event: QKeyEvent) -> bool:
        modifiers = event.modifiers()
        disallowed = modifiers & ~Qt.KeyboardModifier.KeypadModifier
        if disallowed:
            return False

        key = event.key()
        state = self._controller.current_player_state()
        is_video_surface = state in {
            PlayerState.PLAYING_VIDEO,
            PlayerState.SHOWING_VIDEO_SURFACE,
        }
        is_live_motion = state == PlayerState.PLAYING_LIVE_MOTION
        is_live_still = state == PlayerState.SHOWING_LIVE_STILL
        can_control_audio = is_video_surface or is_live_motion

        if key == Qt.Key.Key_Space:
            if is_live_still:
                self._controller.replay_live_photo()
                event.accept()
                return True
            if can_control_audio:
                self._controller.toggle_playback()
                event.accept()
                return True
            return False

        if key == Qt.Key.Key_M and can_control_audio:
            self._controller.set_media_muted(
                not self._controller.is_media_muted()
            )
            event.accept()
            return True

        if key in {Qt.Key.Key_Up, Qt.Key.Key_Down} and can_control_audio:
            step = VOLUME_SHORTCUT_STEP if key == Qt.Key.Key_Up else -VOLUME_SHORTCUT_STEP
            current_volume = self._controller.media_volume()
            new_volume = max(0, min(100, current_volume + step))
            if new_volume != current_volume:
                self._controller.set_media_volume(new_volume)
            event.accept()
            return True

        if key == Qt.Key.Key_Left:
            self._controller.request_previous_item()
            event.accept()
            return True

        if key == Qt.Key.Key_Right:
            self._controller.request_next_item()
            event.accept()
            return True

        return False

