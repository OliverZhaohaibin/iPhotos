"""Controller coordinating the edit view and non-destructive adjustments."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QPixmap

from ....core.image_filters import apply_adjustments
from ....io import sidecar
from ...utils import image_loader
from ..models.asset_model import AssetModel
from ..models.edit_session import EditSession
from ..tasks.thumbnail_loader import ThumbnailLoader
from ..ui_main_window import Ui_MainWindow
from .player_view_controller import PlayerViewController
from .view_controller import ViewController


class EditController(QObject):
    """Own the edit session state and synchronise UI widgets."""

    editingStarted = Signal(Path)
    """Emitted after a source asset has been loaded for editing."""

    editingFinished = Signal(Path)
    """Emitted once adjustments are saved and the detail view is restored."""

    def __init__(
        self,
        ui: Ui_MainWindow,
        view_controller: ViewController,
        player_view: PlayerViewController,
        playlist,
        asset_model: AssetModel,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._ui = ui
        self._view_controller = view_controller
        self._player_view = player_view
        self._playlist = playlist
        self._asset_model = asset_model
        self._thumbnail_loader: ThumbnailLoader = asset_model.thumbnail_loader()

        self._session: Optional[EditSession] = None
        self._base_image: Optional[QImage] = None
        self._current_source: Optional[Path] = None

        ui.edit_reset_button.clicked.connect(self._handle_reset_clicked)
        ui.edit_done_button.clicked.connect(self._handle_done_clicked)
        ui.edit_adjust_action.triggered.connect(lambda checked: self._handle_mode_change("adjust", checked))
        ui.edit_crop_action.triggered.connect(lambda checked: self._handle_mode_change("crop", checked))

        playlist.currentChanged.connect(self._handle_playlist_change)
        playlist.sourceChanged.connect(lambda _path: self._handle_playlist_change())

        ui.edit_header_container.hide()

    # ------------------------------------------------------------------
    def begin_edit(self) -> None:
        """Enter the edit view for the playlist's current asset."""

        if self._view_controller.is_edit_view_active():
            return
        source = self._playlist.current_source()
        if source is None:
            return
        image = image_loader.load_qimage(source)
        if image is None or image.isNull():
            return

        self._base_image = image
        self._current_source = source

        adjustments = sidecar.load_adjustments(source)

        session = EditSession(self)
        session.set_values(adjustments, emit_individual=False)
        session.valuesChanged.connect(self._handle_session_changed)
        self._session = session

        self._ui.edit_sidebar.set_session(session)
        self._ui.edit_sidebar.refresh()
        self._set_mode("adjust")
        self._apply_preview()

        self._ui.detail_chrome_container.hide()
        self._ui.edit_header_container.show()
        self._view_controller.show_edit_view()

        self.editingStarted.emit(source)

    def leave_edit_mode(self) -> None:
        """Return to the standard detail view without persisting changes."""

        if not self._view_controller.is_edit_view_active():
            return
        self._ui.edit_sidebar.set_session(None)
        self._ui.edit_image_viewer.clear()
        self._ui.edit_header_container.hide()
        self._ui.detail_chrome_container.show()
        self._view_controller.show_detail_view()
        self._session = None
        self._base_image = None
        self._current_source = None

    # ------------------------------------------------------------------
    def _handle_session_changed(self, values: dict) -> None:
        del values  # Unused – the session already stores the authoritative mapping.
        self._apply_preview()

    def _apply_preview(self) -> None:
        if self._base_image is None or self._session is None:
            self._ui.edit_image_viewer.clear()
            return
        adjusted = apply_adjustments(self._base_image, self._session.values())
        pixmap = QPixmap.fromImage(adjusted)
        if pixmap.isNull():
            self._ui.edit_image_viewer.clear()
            return
        self._ui.edit_image_viewer.set_pixmap(pixmap)

    def _handle_reset_clicked(self) -> None:
        if self._session is None:
            return
        self._session.reset()

    def _handle_done_clicked(self) -> None:
        if self._session is None or self._current_source is None:
            self.leave_edit_mode()
            return
        adjustments = self._session.values()
        sidecar.save_adjustments(self._current_source, adjustments)
        self._refresh_thumbnail_cache(self._current_source)
        self.leave_edit_mode()
        self._player_view.display_image(self._current_source)
        self.editingFinished.emit(self._current_source)

    def _refresh_thumbnail_cache(self, source: Path) -> None:
        metadata = self._asset_model.source_model().metadata_for_absolute_path(source)
        if metadata is None:
            return
        rel_value = metadata.get("rel")
        if not rel_value:
            return
        rel = str(rel_value)
        source_model = self._asset_model.source_model()
        if hasattr(source_model, "invalidate_thumbnail"):
            source_model.invalidate_thumbnail(rel)
        self._thumbnail_loader.invalidate(rel)

    def _handle_mode_change(self, mode: str, checked: bool) -> None:
        if not checked:
            return
        self._set_mode(mode)

    def _set_mode(self, mode: str) -> None:
        if mode == "adjust":
            self._ui.edit_adjust_action.setChecked(True)
            self._ui.edit_crop_action.setChecked(False)
            self._ui.edit_sidebar.set_mode("adjust")
        else:
            self._ui.edit_adjust_action.setChecked(False)
            self._ui.edit_crop_action.setChecked(True)
            self._ui.edit_sidebar.set_mode("crop")

    def _handle_playlist_change(self) -> None:
        if self._view_controller.is_edit_view_active():
            self.leave_edit_mode()
