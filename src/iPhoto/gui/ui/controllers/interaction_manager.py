"""Controllers focused on user interaction flows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

from ..widgets import NotificationToast
from .context_menu_controller import ContextMenuController
from .drag_drop_controller import DragDropController
from .playback_controller import PlaybackController
from .playback_state_manager import PlaybackStateManager
from .preference_controller import PreferenceController
from .preview_controller import PreviewController
from .selection_controller import SelectionController
from .share_controller import ShareController
from .shortcut_controller import ShortcutController

if TYPE_CHECKING:  # pragma: no cover - import used for typing only
    from ....appctx import AppContext
    from ...facade import AppFacade
    from ..main_window import MainWindow
    from ..window_manager import FramelessWindowManager
    from ..ui_main_window import Ui_MainWindow
    from .data_manager import DataManager
    from .dialog_controller import DialogController
    from .navigation_controller import NavigationController
    from .status_bar_controller import StatusBarController
    from .view_controller_manager import ViewControllerManager
    from .main_controller import MainController


class InteractionManager(QObject):
    """Aggregate controllers that manage direct user input."""

    def __init__(
        self,
        *,
        window: "MainWindow",
        context: AppContext,
        facade: AppFacade,
        data_manager: DataManager,
        view_manager: ViewControllerManager,
        navigation: NavigationController,
        dialog: DialogController,
        status_bar: StatusBarController,
        window_manager: FramelessWindowManager,
        main_controller: "MainController",
    ) -> None:
        super().__init__(window)
        ui: Ui_MainWindow = window.ui

        self._preview = PreviewController(ui.preview_window, window)
        self._state_manager = PlaybackStateManager(
            data_manager.media(),
            data_manager.playlist(),
            data_manager.asset_model(),
            view_manager.detail_ui(),
            dialog,
            window,
        )
        self._playback = PlaybackController(
            data_manager.asset_model(),
            data_manager.media(),
            data_manager.playlist(),
            ui.grid_view,
            view_manager.view_controller(),
            view_manager.detail_ui(),
            self._state_manager,
            self._preview,
            facade,
        )
        navigation.bind_playback_controller(self._playback)

        self._notification_toast = NotificationToast(window)
        self._selection = SelectionController(
            ui.selection_button,
            ui.grid_view,
            data_manager.grid_delegate(),
            self._preview,
            self._playback,
            parent=window,
        )
        self._preference_controller = PreferenceController(
            settings=context.settings,
            media=data_manager.media(),
            player_bar=ui.player_bar,
            filmstrip_view=ui.filmstrip_view,
            filmstrip_action=ui.toggle_filmstrip_action,
            wheel_action_group=ui.wheel_action_group,
            wheel_action_zoom=ui.wheel_action_zoom,
            wheel_action_navigate=ui.wheel_action_navigate,
            image_viewer=ui.image_viewer,
            parent=window,
        )
        self._share = ShareController(
            settings=context.settings,
            playlist=data_manager.playlist(),
            asset_model=data_manager.asset_model(),
            status_bar=status_bar,
            notification_toast=self._notification_toast,
            share_button=ui.share_button,
            share_action_group=ui.share_action_group,
            copy_file_action=ui.share_action_copy_file,
            copy_path_action=ui.share_action_copy_path,
            reveal_action=ui.share_action_reveal_file,
            parent=window,
        )
        self._share.restore_preference()

        self._context_menu = ContextMenuController(
            grid_view=ui.grid_view,
            asset_model=data_manager.asset_model(),
            facade=facade,
            navigation=navigation,
            status_bar=status_bar,
            notification_toast=self._notification_toast,
            selection_controller=self._selection,
            parent=window,
        )
        self._drag_drop = DragDropController(
            grid_view=ui.grid_view,
            sidebar=ui.sidebar,
            context=context,
            facade=facade,
            status_bar=status_bar,
            dialog=dialog,
            navigation=navigation,
            parent=window,
        )
        self._shortcut = ShortcutController(
            window,
            window_manager,
            main_controller,
            view_manager,
            navigation,
            self._context_menu,
        )

    # ------------------------------------------------------------------
    # Accessors
    def playback(self) -> PlaybackController:
        return self._playback

    def state_manager(self) -> PlaybackStateManager:
        return self._state_manager

    def preview(self) -> PreviewController:
        return self._preview

    def selection(self) -> SelectionController:
        return self._selection

    def share(self) -> ShareController:
        return self._share

    def context_menu(self) -> ContextMenuController:
        return self._context_menu

    def drag_drop(self) -> DragDropController:
        return self._drag_drop

    def preference_controller(self) -> PreferenceController:
        return self._preference_controller

    def shortcut(self) -> ShortcutController:
        return self._shortcut

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Release resources held by interaction controllers."""

        self._shortcut.shutdown()

