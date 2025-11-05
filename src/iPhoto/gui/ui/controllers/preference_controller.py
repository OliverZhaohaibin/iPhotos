"""Controller that encapsulates UI preference persistence."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QWidget

from ..media import MediaController
from ..widgets.player_bar import PlayerBar
from ..widgets.gl_image_viewer import GLImageViewer


class PreferenceController(QObject):
    """Synchronise persisted preferences with their widgets."""

    def __init__(
        self,
        *,
        settings,
        media: MediaController,
        player_bar: PlayerBar,
        filmstrip_view: QWidget,
        filmstrip_action: QAction,
        wheel_action_group: QActionGroup,
        wheel_action_zoom: QAction,
        wheel_action_navigate: QAction,
        image_viewer: GLImageViewer,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._media = media
        self._player_bar = player_bar
        self._filmstrip_view = filmstrip_view
        self._filmstrip_action = filmstrip_action
        self._wheel_action_group = wheel_action_group
        self._wheel_action_zoom = wheel_action_zoom
        self._wheel_action_navigate = wheel_action_navigate
        self._image_viewer = image_viewer

        self.restore_playback_preferences()
        self.restore_wheel_preference()

        self._filmstrip_action.toggled.connect(self._handle_toggle_filmstrip)
        self._wheel_action_group.triggered.connect(self._handle_wheel_action_changed)

    # ------------------------------------------------------------------
    # Preference restoration
    # ------------------------------------------------------------------
    def restore_playback_preferences(self) -> None:
        stored_volume = self._settings.get("ui.volume", 75)
        try:
            initial_volume = int(round(float(stored_volume)))
        except (TypeError, ValueError):
            initial_volume = 75
        initial_volume = max(0, min(100, initial_volume))

        stored_muted = self._settings.get("ui.is_muted", False)
        if isinstance(stored_muted, str):
            initial_muted = stored_muted.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            initial_muted = bool(stored_muted)

        stored_filmstrip = self._settings.get("ui.show_filmstrip", True)
        if isinstance(stored_filmstrip, str):
            show_filmstrip = stored_filmstrip.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            show_filmstrip = bool(stored_filmstrip)

        self._media.set_volume(initial_volume)
        self._media.set_muted(initial_muted)
        self._player_bar.set_volume(self._media.volume())
        self._player_bar.set_muted(self._media.is_muted())
        self._filmstrip_view.setVisible(show_filmstrip)
        self._filmstrip_action.setChecked(show_filmstrip)

    def restore_wheel_preference(self) -> None:
        wheel_action = self._settings.get("ui.wheel_action", "navigate")
        if wheel_action == "zoom":
            self._wheel_action_zoom.setChecked(True)
        else:
            wheel_action = "navigate"
            self._wheel_action_navigate.setChecked(True)
        self._apply_wheel_action(wheel_action)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    def _handle_toggle_filmstrip(self, checked: bool) -> None:
        show_filmstrip = bool(checked)
        self._filmstrip_view.setVisible(show_filmstrip)
        if self._settings.get("ui.show_filmstrip") != show_filmstrip:
            self._settings.set("ui.show_filmstrip", show_filmstrip)

    def _handle_wheel_action_changed(self, action: QAction) -> None:
        if action is self._wheel_action_zoom:
            selected = "zoom"
        else:
            selected = "navigate"
        if self._settings.get("ui.wheel_action") != selected:
            self._settings.set("ui.wheel_action", selected)
        self._apply_wheel_action(selected)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _apply_wheel_action(self, action: str) -> None:
        mode = "zoom" if action == "zoom" else "navigate"
        self._image_viewer.set_wheel_action(mode)
