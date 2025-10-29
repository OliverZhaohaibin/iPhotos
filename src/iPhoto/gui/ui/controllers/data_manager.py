"""Data-centric helpers used by the main window controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt

from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel
from ..models.spacer_proxy_model import SpacerProxyModel
from ..widgets.asset_delegate import AssetGridDelegate

if TYPE_CHECKING:  # pragma: no cover - import used for typing only
    from ..ui_main_window import Ui_MainWindow
    from ...facade import AppFacade
    from ..main_window import MainWindow


class DataManager(QObject):
    """Own the core models and data controllers used across the UI."""

    def __init__(self, window: "MainWindow", facade: "AppFacade") -> None:
        super().__init__(window)
        self._window = window
        self._facade = facade

        self._asset_model = AssetModel(self._facade)
        self._filmstrip_model = SpacerProxyModel(window)
        self._filmstrip_model.setSourceModel(self._asset_model)

        self._media = MediaController(window)
        self._playlist = PlaylistController(window)
        self._grid_delegate: AssetGridDelegate | None = None

    # ------------------------------------------------------------------
    # Model exposure
    def asset_model(self) -> AssetModel:
        """Return the gallery's data model."""

        return self._asset_model

    def filmstrip_model(self) -> SpacerProxyModel:
        """Return the proxy model used by the filmstrip view."""

        return self._filmstrip_model

    def media(self) -> MediaController:
        """Expose the multimedia backend wrapper."""

        return self._media

    def playlist(self) -> PlaylistController:
        """Expose the playlist controller shared between the views."""

        return self._playlist

    def grid_delegate(self) -> AssetGridDelegate | None:
        """Return the grid delegate once it has been created."""

        return self._grid_delegate

    # ------------------------------------------------------------------
    # View configuration helpers
    def configure_views(self, ui: "Ui_MainWindow") -> None:
        """Attach models and delegates to the widgets constructed by the UI."""

        ui.grid_view.setModel(self._asset_model)
        self._grid_delegate = AssetGridDelegate(ui.grid_view)
        ui.grid_view.setItemDelegate(self._grid_delegate)
        ui.grid_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        ui.filmstrip_view.setModel(self._filmstrip_model)
        ui.filmstrip_view.setItemDelegate(
            AssetGridDelegate(ui.filmstrip_view, filmstrip_mode=True)
        )

        ui.video_area.hide_controls(animate=False)
        self._media.set_video_output(ui.video_area.video_item)

        ui.player_bar.setEnabled(False)
        ui.selection_button.setEnabled(False)

