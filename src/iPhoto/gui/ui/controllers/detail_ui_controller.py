"""Utilities for keeping the detail page widgets in sync with the playlist."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QModelIndex, QItemSelectionModel, QObject, QTimer
from PySide6.QtWidgets import QStatusBar, QToolButton

from ..icons import load_icon
from ..models.asset_model import AssetModel, Roles
from ..widgets.asset_grid import AssetGrid
from ..widgets.player_bar import PlayerBar
from .header_controller import HeaderController
from .player_view_controller import PlayerViewController
from .view_controller import ViewController


class DetailUIController(QObject):
    """Manage the collection of widgets that form the detail page."""

    def __init__(
        self,
        model: AssetModel,
        filmstrip_view: AssetGrid,
        player_view: PlayerViewController,
        player_bar: PlayerBar,
        view_controller: ViewController,
        header: HeaderController,
        favorite_button: QToolButton,
        status_bar: QStatusBar,
        parent: QObject | None = None,
    ) -> None:
        """Store widget references and apply the initial UI baseline."""

        super().__init__(parent)
        self._model = model
        self._filmstrip_view = filmstrip_view
        self._player_view = player_view
        self._player_bar = player_bar
        self._view_controller = view_controller
        self._header = header
        self._favorite_button = favorite_button
        self._status_bar = status_bar

        self._initialize_static_state()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def handle_playlist_current_changed(self, current_row: int, previous_row: int) -> None:
        """Synchronise the detail UI when the playlist focus changes."""

        self.update_favorite_button(current_row)

        selection_model = self._filmstrip_view.selectionModel()
        if selection_model is None:
            return
        filmstrip_model = self._filmstrip_view.model()
        if filmstrip_model is None:
            return

        source_model = self._model.source_model()

        def _set_is_current(proxy_row: int, value: bool) -> None:
            if proxy_row < 0:
                return
            proxy_index = self._model.index(proxy_row, 0)
            if not proxy_index.isValid():
                return
            source_index = self._model.mapToSource(proxy_index)
            if not source_index.isValid():
                return
            source_model.setData(source_index, value, Roles.IS_CURRENT)

        _set_is_current(previous_row, False)
        if current_row >= 0:
            _set_is_current(current_row, True)

        self._header.update_for_row(current_row if current_row >= 0 else None, self._model)

        proxy_index: Optional[QModelIndex] = None
        if current_row >= 0:
            proxy_row = current_row + 1
            candidate = filmstrip_model.index(proxy_row, 0)
            if candidate.isValid():
                proxy_index = candidate

        self._filmstrip_view.refresh_spacers(proxy_index)
        if current_row < 0:
            self._player_bar.setEnabled(False)
            self._player_view.show_placeholder()
            selection_model.clearSelection()
            return

        selection_model.clearSelection()
        if proxy_index is None:
            return
        selection_model.select(
            proxy_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(
            proxy_index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        self._filmstrip_view.refresh_spacers(proxy_index)
        QTimer.singleShot(0, lambda idx=proxy_index: self._filmstrip_view.center_on_index(idx))
        self._player_bar.setEnabled(True)
        self._view_controller.show_detail_view()
        self._filmstrip_view.doItemsLayout()

    def update_favorite_button(
        self, row: int, *, is_featured: Optional[bool] = None
    ) -> None:
        """Reflect the featured state of *row* on the favorite button."""

        if row < 0:
            self._favorite_button.setEnabled(False)
            self._favorite_button.setIcon(load_icon("suit.heart.svg"))
            self._favorite_button.setToolTip("Add to Favorites")
            return

        index = self._model.index(row, 0)
        if not index.isValid():
            self._favorite_button.setEnabled(False)
            self._favorite_button.setIcon(load_icon("suit.heart.svg"))
            self._favorite_button.setToolTip("Add to Favorites")
            return

        featured_state = is_featured
        if featured_state is None:
            featured_state = bool(index.data(Roles.FEATURED))

        icon_name = "suit.heart.fill.svg" if featured_state else "suit.heart.svg"
        tooltip = "Remove from Favorites" if featured_state else "Add to Favorites"
        self._favorite_button.setEnabled(True)
        self._favorite_button.setIcon(load_icon(icon_name))
        self._favorite_button.setToolTip(tooltip)

    def update_header(self, row: Optional[int]) -> None:
        """Update the header metadata for *row*."""

        self._header.update_for_row(row if row is not None else None, self._model)

    def show_detail_view(self) -> None:
        """Ensure the detail page is visible."""

        self._view_controller.show_detail_view()

    def show_placeholder(self) -> None:
        """Swap the player area back to its placeholder state."""

        self._player_view.show_placeholder()
        self._player_bar.setEnabled(False)

    def show_live_badge(self) -> None:
        """Expose a convenient wrapper for displaying the Live Photo badge."""

        self._player_view.show_live_badge()

    def hide_live_badge(self) -> None:
        """Hide the Live Photo badge."""

        self._player_view.hide_live_badge()

    def set_live_replay_enabled(self, enabled: bool) -> None:
        """Toggle the Live Photo replay affordance."""

        self._player_view.set_live_replay_enabled(enabled)

    def select_filmstrip_row(self, row: int) -> None:
        """Select *row* in the filmstrip view if it exists."""

        selection_model = self._filmstrip_view.selectionModel()
        if selection_model is None or row < 0:
            return
        filmstrip_model = self._filmstrip_view.model()
        if filmstrip_model is None:
            return
        proxy_row = row + 1
        proxy_index = filmstrip_model.index(proxy_row, 0)
        if not proxy_index.isValid():
            return
        selection_model.setCurrentIndex(
            proxy_index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        selection_model.select(
            proxy_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._filmstrip_view.refresh_spacers(proxy_index)
        QTimer.singleShot(0, lambda idx=proxy_index: self._filmstrip_view.center_on_index(idx))

    def reset_for_gallery_view(self) -> None:
        """Clear detail-specific UI when switching back to the gallery."""

        self._player_bar.setEnabled(False)
        self._player_view.show_placeholder()
        self._header.clear()
        self.update_favorite_button(-1)
        self._filmstrip_view.clearSelection()
        self._status_bar.showMessage("Browse your library")

    def show_status_message(self, message: str) -> None:
        """Mirror *message* to the status bar."""

        self._status_bar.showMessage(message)

    def set_player_bar_enabled(self, enabled: bool) -> None:
        """Toggle the enabled state of the player bar."""

        self._player_bar.setEnabled(enabled)

    def reset_player_bar(self) -> None:
        """Reset the player bar to its default position and duration."""

        self._player_bar.reset()

    @property
    def player_bar(self) -> PlayerBar:
        """Expose the managed player bar instance."""

        return self._player_bar

    @property
    def player_view(self) -> PlayerViewController:
        """Expose the managed player view controller."""

        return self._player_view

    @property
    def filmstrip_view(self) -> AssetGrid:
        """Expose the managed filmstrip view."""

        return self._filmstrip_view

    @property
    def favorite_button(self) -> QToolButton:
        """Expose the favorite button so higher-level controllers can wire slots."""

        return self._favorite_button

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _initialize_static_state(self) -> None:
        """Apply the default state expected when the controller is created."""

        self._header.clear()
        self._player_view.hide_live_badge()
        self._player_view.set_live_replay_enabled(False)
        self._favorite_button.setEnabled(False)
        self._favorite_button.setIcon(load_icon("suit.heart.svg"))
        self._favorite_button.setToolTip("Add to Favorites")

    @property
    def status_bar(self) -> QStatusBar:
        """Provide access to the managed status bar."""

        return self._status_bar
