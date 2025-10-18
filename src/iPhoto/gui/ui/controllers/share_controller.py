"""Controller dedicated to share-related toolbar interactions."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QMimeData, QUrl
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QPushButton
from PySide6.QtGui import QActionGroup

from ..models.asset_model import AssetModel, Roles
from ..widgets.notification_toast import NotificationToast
from .playlist_controller import PlaylistController
from .status_bar_controller import StatusBarController


class ShareController(QObject):
    """Encapsulate the share button workflow used by the main window."""

    def __init__(
        self,
        *,
        settings,
        playlist: PlaylistController,
        asset_model: AssetModel,
        status_bar: StatusBarController,
        notification_toast: NotificationToast,
        share_button: QPushButton,
        share_action_group: QActionGroup,
        copy_file_action: QAction,
        copy_path_action: QAction,
        reveal_action: QAction,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._playlist = playlist
        self._asset_model = asset_model
        self._status_bar = status_bar
        self._toast = notification_toast
        self._share_button = share_button
        self._share_action_group = share_action_group
        self._copy_file_action = copy_file_action
        self._copy_path_action = copy_path_action
        self._reveal_action = reveal_action

        self._share_action_group.triggered.connect(self._handle_action_changed)
        self._share_button.clicked.connect(self._handle_share_requested)

    # ------------------------------------------------------------------
    # Preference lifecycle
    # ------------------------------------------------------------------
    def restore_preference(self) -> None:
        """Apply the persisted share choice to the action group."""

        share_action = self._settings.get("ui.share_action", "reveal_file")
        mapping = {
            "copy_file": self._copy_file_action,
            "copy_path": self._copy_path_action,
            "reveal_file": self._reveal_action,
        }
        target = mapping.get(share_action, self._reveal_action)
        target.setChecked(True)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _handle_action_changed(self, action: QAction) -> None:
        if action is self._copy_file_action:
            self._settings.set("ui.share_action", "copy_file")
        elif action is self._copy_path_action:
            self._settings.set("ui.share_action", "copy_path")
        else:
            self._settings.set("ui.share_action", "reveal_file")

    def _handle_share_requested(self) -> None:
        current_row = self._playlist.current_row()
        if current_row < 0:
            self._status_bar.show_message("No item selected to share.", 3000)
            return

        index = self._asset_model.index(current_row, 0)
        if not index.isValid():
            return

        file_path_str = index.data(Roles.ABS)
        if not file_path_str:
            return

        file_path = Path(file_path_str)
        share_action = self._settings.get("ui.share_action", "reveal_file")

        if share_action == "copy_file":
            self._copy_file_to_clipboard(file_path)
        elif share_action == "copy_path":
            self._copy_path_to_clipboard(file_path)
        else:
            self._reveal_in_file_manager(file_path)

    # ------------------------------------------------------------------
    # Clipboard helpers
    # ------------------------------------------------------------------
    def _copy_file_to_clipboard(self, path: Path) -> None:
        if not path.exists():
            self._status_bar.show_message(f"File not found: {path.name}", 3000)
            return

        mime_data = self._build_file_mime_data(path)
        QGuiApplication.clipboard().setMimeData(mime_data)
        self._toast.show_toast("Copied to Clipboard")

    def _copy_path_to_clipboard(self, path: Path) -> None:
        QGuiApplication.clipboard().setText(str(path))
        self._toast.show_toast("Copied to Clipboard")

    def _reveal_in_file_manager(self, path: Path) -> None:
        if not path.exists():
            self._status_bar.show_message(f"File not found: {path.name}", 3000)
            return

        if sys.platform == "win32":
            subprocess.run(["explorer", "/select,", str(path)], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path.parent)], check=False)
        self._status_bar.show_message(f"Revealed {path.name} in file manager.", 3000)

    def _build_file_mime_data(self, path: Path) -> QMimeData:
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(str(path))])
        return mime_data
