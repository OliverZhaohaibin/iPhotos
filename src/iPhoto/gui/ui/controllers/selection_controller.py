"""Controller dedicated to multi-selection mode handling for the gallery grid."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QModelIndex, QObject, QCoreApplication, Signal
from PySide6.QtWidgets import QPushButton

from ..widgets.asset_grid import AssetGrid
from ..widgets.asset_delegate import AssetGridDelegate
from .preview_controller import PreviewController
from .playback_controller import PlaybackController


class SelectionController(QObject):
    """Manage the gallery's selection mode separate from the main coordinator."""

    selectionModeChanged = Signal(bool)

    def __init__(
        self,
        selection_button: QPushButton,
        grid_view: AssetGrid,
        grid_delegate: AssetGridDelegate | None,
        preview_controller: PreviewController,
        playback_controller: PlaybackController,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._selection_button = selection_button
        self._grid_view = grid_view
        self._grid_delegate = grid_delegate
        self._preview_controller = preview_controller
        self._playback = playback_controller
        self._active = False

        self._selection_button.clicked.connect(self._handle_toggle_requested)
        self._grid_view.itemClicked.connect(self._handle_grid_item_clicked)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_selection_mode(self, enabled: bool) -> None:
        """Enable or disable multi-selection mode.

        The method keeps the grid widget, delegate, and toggle button in sync
        so the rest of the UI can simply query :meth:`is_active` and react to
        the change.  When selection mode is disabled the current selection is
        cleared to avoid leaving stale highlights behind.
        """

        desired_state = bool(enabled)
        if self._active == desired_state:
            if not desired_state:
                self._grid_view.clearSelection()
            return

        self._active = desired_state
        self._update_button_state(desired_state)

        self._grid_view.set_selection_mode_enabled(desired_state)
        if self._grid_delegate is not None:
            self._grid_delegate.set_selection_mode_active(desired_state)
        self._grid_view.clearSelection()
        if not desired_state:
            self._preview_controller.close_preview(False)

        self.selectionModeChanged.emit(self._active)

    def is_active(self) -> bool:
        """Return ``True`` when multi-selection mode is currently enabled."""

        return self._active

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _update_button_state(self, enabled: bool) -> None:
        if enabled:
            self._selection_button.setText(
                QCoreApplication.translate("MainWindow", "Cancel")
            )
            self._selection_button.setToolTip(
                QCoreApplication.translate("MainWindow", "Exit multi-selection mode")
            )
        else:
            self._selection_button.setText(
                QCoreApplication.translate("MainWindow", "Select")
            )
            self._selection_button.setToolTip(
                QCoreApplication.translate("MainWindow", "Toggle multi-selection mode")
            )

    def _handle_toggle_requested(self) -> None:
        if not self._selection_button.isEnabled():
            return
        self.set_selection_mode(not self._active)

    def _handle_grid_item_clicked(self, index: QModelIndex) -> None:
        if self._active:
            return
        self._playback.activate_index(index)
