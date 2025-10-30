"""Group view-related controllers used by :class:`MainController`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

from ..widgets import InfoPanel
from .detail_ui_controller import DetailUIController
from .header_controller import HeaderController
from .map_view_controller import LocationMapController
from .edit_controller import EditController
from .player_view_controller import PlayerViewController
from .view_controller import ViewController

if TYPE_CHECKING:  # pragma: no cover - import used for typing only
    from ...appctx import AppContext
    from ..main_window import MainWindow
    from ..ui_main_window import Ui_MainWindow
    from ...facade import AppFacade
    from .data_manager import DataManager


class ViewControllerManager(QObject):
    """Own controllers responsible for view presentation and transitions."""

    def __init__(
        self,
        window: "MainWindow",
        context: AppContext,
        data_manager: "DataManager",
    ) -> None:
        super().__init__(window)
        ui: Ui_MainWindow = window.ui

        self._view_controller = ViewController(
            ui.view_stack,
            ui.gallery_page,
            ui.detail_page,
            ui.edit_page,
            map_page=ui.map_page,
            parent=window,
        )
        self._player_view = PlayerViewController(
            ui.player_stack,
            ui.image_viewer,
            ui.video_area,
            ui.player_placeholder,
            ui.live_badge,
            window,
        )
        self._header = HeaderController(ui.location_label, ui.timestamp_label)
        self._info_panel = InfoPanel(window)
        self._detail_ui = DetailUIController(
            data_manager.asset_model(),
            ui.filmstrip_view,
            self._player_view,
            ui.player_bar,
            self._view_controller,
            self._header,
            ui.favorite_button,
            ui.edit_button,
            ui.info_button,
            self._info_panel,
            ui.zoom_widget,
            ui.zoom_slider,
            ui.zoom_in_button,
            ui.zoom_out_button,
            ui.status_bar,
            window,
        )
        self._edit_controller = EditController(
            ui,
            self._view_controller,
            self._player_view,
            data_manager.playlist(),
            data_manager.asset_model(),
            window,
        )
        self._map_controller = LocationMapController(
            context.library,
            data_manager.playlist(),
            self._view_controller,
            ui.map_view,
            window,
        )

    # ------------------------------------------------------------------
    # View controller accessors
    def view_controller(self) -> ViewController:
        return self._view_controller

    def player_view(self) -> PlayerViewController:
        return self._player_view

    def detail_ui(self) -> DetailUIController:
        return self._detail_ui

    def header(self) -> HeaderController:
        return self._header

    def map_controller(self) -> LocationMapController:
        return self._map_controller

    def edit_controller(self) -> EditController:
        return self._edit_controller

    # ------------------------------------------------------------------
    # Convenience wrappers
    def is_detail_view_active(self) -> bool:
        """Return ``True`` when the detail page is currently visible."""

        return self._view_controller.is_detail_view_active()

    def show_gallery_view(self) -> None:
        """Switch the stacked widget back to the gallery view."""

        self._view_controller.show_gallery_view()

    def show_detail_view(self) -> None:
        """Switch the stacked widget to the detail page."""

        self._view_controller.show_detail_view()

