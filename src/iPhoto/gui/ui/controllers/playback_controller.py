"""Coordinate playback, preview, and detail view presentation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QRect, Qt
from PySide6.QtWidgets import QStackedWidget, QStatusBar, QWidget

from ....config import VIDEO_COMPLETE_HOLD_BACKSTEP_MS
from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel, Roles
from ...utils import image_loader
from ..widgets.asset_grid import AssetGrid
from ..widgets.image_viewer import ImageViewer
from ..widgets.player_bar import PlayerBar
from ..widgets.video_area import VideoArea
from ..widgets.preview_window import PreviewWindow
from .dialog_controller import DialogController


class PlaybackController:
    """Encapsulate playback state and UI coordination."""

    def __init__(
        self,
        model: AssetModel,
        media: MediaController,
        playlist: PlaylistController,
        player_bar: PlayerBar,
        video_area: VideoArea,
        grid_view: AssetGrid,
        filmstrip_view: AssetGrid,
        player_stack: QStackedWidget,
        image_viewer: ImageViewer,
        player_placeholder: QWidget,
        view_stack: QStackedWidget,
        gallery_page: QWidget,
        detail_page: QWidget,
        preview_window: PreviewWindow,
        status_bar: QStatusBar,
        dialog: DialogController,
    ) -> None:
        self._model = model
        self._media = media
        self._playlist = playlist
        self._player_bar = player_bar
        self._video_area = video_area
        self._grid_view = grid_view
        self._filmstrip_view = filmstrip_view
        self._player_stack = player_stack
        self._image_viewer = image_viewer
        self._player_placeholder = player_placeholder
        self._view_stack = view_stack
        self._gallery_page = gallery_page
        self._detail_page = detail_page
        self._preview_window = preview_window
        self._status = status_bar
        self._dialog = dialog
        self._resume_playback_after_scrub = False
        self._live_preview_active = False
        self._live_preview_still: Path | None = None
        self._live_preview_row: int | None = None

    # ------------------------------------------------------------------
    # Selection handling
    # ------------------------------------------------------------------
    def activate_index(self, index: QModelIndex) -> None:
        if not index or not index.isValid():
            return
        abs_raw = index.data(Roles.ABS)
        if not abs_raw:
            return
        row = index.row()
        abs_path = Path(str(abs_raw))
        is_video = bool(index.data(Roles.IS_VIDEO))
        is_live = bool(index.data(Roles.IS_LIVE))
        if is_video or is_live:
            fallback_live = False
            if is_live:
                self._display_image(abs_path, row=row)
                self._live_preview_active = True
                self._live_preview_still = abs_path
                self._live_preview_row = row
            self.show_detail_view()
            source = self._playlist.set_current(row)
            if source is None and is_live:
                motion_raw = index.data(Roles.LIVE_MOTION_ABS)
                motion_path = (
                    Path(str(motion_raw))
                    if isinstance(motion_raw, (str, Path)) and str(motion_raw)
                    else None
                )
                self._display_live_photo(abs_path, motion_path, row=row)
                fallback_live = True
            if source is None and not fallback_live:
                self._cancel_live_preview()
            return
        self._display_image(abs_path, row=row)

    def show_preview_for_index(self, view: AssetGrid, index: QModelIndex) -> None:
        if not index or not index.isValid():
            return
        is_video = bool(index.data(Roles.IS_VIDEO))
        is_live = bool(index.data(Roles.IS_LIVE))
        if not is_video and not is_live:
            return
        preview_raw = None
        if is_live:
            preview_raw = index.data(Roles.LIVE_MOTION_ABS)
        else:
            preview_raw = index.data(Roles.ABS)
        if not preview_raw:
            return
        preview_path = Path(str(preview_raw))
        rect = view.visualRect(index)
        global_rect = QRect(view.viewport().mapToGlobal(rect.topLeft()), rect.size())
        self._preview_window.show_preview(preview_path, global_rect)

    def close_preview_after_release(self) -> None:
        self._preview_window.close_preview()

    def cancel_preview(self) -> None:
        self._preview_window.close_preview(False)

    # ------------------------------------------------------------------
    # Playlist callbacks
    # ------------------------------------------------------------------
    def handle_playlist_current_changed(self, row: int) -> None:
        selection_model = self._filmstrip_view.selectionModel()
        if selection_model is None:
            return
        if row < 0:
            self._player_bar.reset()
            self._player_bar.setEnabled(False)
            self._media.stop()
            self._show_player_placeholder()
            selection_model.clearSelection()
            return
        selection_model.clearSelection()
        index = self._model.index(row, 0)
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        self._filmstrip_view.scrollTo(index)
        self._player_bar.setEnabled(True)
        self.show_detail_view()

    def handle_playlist_source_changed(self, source: Path) -> None:
        preview_mode = self._live_preview_active
        if not preview_mode:
            self._cancel_live_preview()
        self._preview_window.close_preview(False)
        self._media.stop()
        self._media.load(source)
        if preview_mode:
            self._player_bar.reset()
        self._player_bar.set_position(0)
        self._player_bar.set_duration(0)
        self._show_video_surface(interactive=not preview_mode)
        self.show_detail_view()
        self._media.play()
        if preview_mode and self._live_preview_still is not None:
            self._status.showMessage(f"Playing Live Photo {self._live_preview_still.name}")
        else:
            self._status.showMessage(f"Playing {source.name}")

    # ------------------------------------------------------------------
    # Media callbacks
    # ------------------------------------------------------------------
    def handle_media_status_changed(self, status: object) -> None:
        name = getattr(status, "name", None)
        if self._live_preview_active:
            if name == "EndOfMedia":
                self._finish_live_preview()
            elif name in {"LoadedMedia", "BufferingMedia", "BufferedMedia"}:
                self._video_area.hide_controls(animate=False)
            return
        if name == "EndOfMedia":
            self._freeze_video_final_frame()
            return
        if name in {"LoadedMedia", "BufferingMedia", "BufferedMedia", "StalledMedia"}:
            self._video_area.note_activity()

    def handle_media_position_changed(self, position_ms: int) -> None:
        self._player_bar.set_position(position_ms)

    # ------------------------------------------------------------------
    # Player bar events
    # ------------------------------------------------------------------
    def toggle_playback(self) -> None:
        if self._player_stack.currentWidget() is self._video_area:
            self._video_area.note_activity()
        state = self._media.playback_state()
        playing = getattr(state, "name", None) == "PlayingState"
        if not playing:
            duration = self._player_bar.duration()
            if duration > 0 and self._player_bar.position() >= duration:
                self._media.seek(0)
                self._player_bar.set_position(0)
        self._media.toggle()

    def on_scrub_started(self) -> None:
        state = self._media.playback_state()
        self._resume_playback_after_scrub = getattr(state, "name", "") == "PlayingState"
        if self._resume_playback_after_scrub:
            self._media.pause()

    def on_scrub_finished(self) -> None:
        if self._resume_playback_after_scrub:
            self._media.play()
        self._resume_playback_after_scrub = False

    # ------------------------------------------------------------------
    # View helpers
    # ------------------------------------------------------------------
    def show_gallery_view(self) -> None:
        self._cancel_live_preview()
        self._preview_window.close_preview(False)
        self._media.stop()
        self._playlist.clear()
        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._show_player_placeholder()
        self._image_viewer.clear()
        self._filmstrip_view.clearSelection()
        self._grid_view.clearSelection()
        if self._gallery_page is not None:
            self._view_stack.setCurrentWidget(self._gallery_page)
        self._status.showMessage("Browse your library")

    def show_detail_view(self) -> None:
        if self._detail_page is not None and self._view_stack.currentWidget() is not self._detail_page:
            self._view_stack.setCurrentWidget(self._detail_page)

    def select_filmstrip_row(self, row: int) -> None:
        selection_model = self._filmstrip_view.selectionModel()
        if selection_model is None or row < 0:
            return
        index = self._model.index(row, 0)
        selection_model.setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._filmstrip_view.scrollTo(index)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _display_live_photo(self, still: Path, motion: Path | None, row: int) -> None:
        self._display_image(still, row=row)
        if motion is None:
            self._status.showMessage(f"Viewing {still.name}")
            return
        if not motion.exists():
            self._status.showMessage(f"Live video missing for {still.name}")
            return

        self._live_preview_active = True
        self._live_preview_still = still
        self._live_preview_row = row
        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._video_area.hide_controls(animate=False)
        self._media.load(motion)
        self._show_video_surface(interactive=False)
        self._media.play()
        self._status.showMessage(f"Playing Live Photo {still.name}")

    def _display_image(self, source: Path, row: int | None = None) -> None:
        self._cancel_live_preview()
        pixmap = image_loader.load_qpixmap(source)
        if pixmap is None:
            self._status.showMessage(f"Unable to display {source.name}")
            self._dialog.show_error(f"Could not load {source}")
            return
        self._preview_window.close_preview(False)
        self._media.stop()
        self._playlist.clear()
        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._image_viewer.set_pixmap(pixmap)
        self._show_image_surface()
        self.show_detail_view()
        if row is not None:
            self.select_filmstrip_row(row)
        self._status.showMessage(f"Viewing {source.name}")

    def _show_player_placeholder(self) -> None:
        self._cancel_live_preview()
        self._video_area.hide_controls(animate=False)
        self._resume_playback_after_scrub = False
        if self._player_stack.currentWidget() is not self._player_placeholder:
            self._player_stack.setCurrentWidget(self._player_placeholder)
        self._image_viewer.clear()

    def _show_video_surface(self, *, interactive: bool = True) -> None:
        if self._player_stack.currentWidget() is not self._video_area:
            self._player_stack.setCurrentWidget(self._video_area)
        self._resume_playback_after_scrub = False
        if interactive:
            self._player_bar.setEnabled(True)
            self._video_area.show_controls(animate=False)
        else:
            self._player_bar.setEnabled(False)
            self._video_area.hide_controls(animate=False)

    def _freeze_video_final_frame(self) -> None:
        if self._live_preview_active:
            return
        if self._player_stack.currentWidget() is not self._video_area:
            return
        duration = self._player_bar.duration()
        if duration <= 0:
            return
        backstep = max(0, VIDEO_COMPLETE_HOLD_BACKSTEP_MS)
        target = max(0, duration - backstep)
        self._media.seek(target)
        self._media.pause()
        self._player_bar.set_position(duration)
        self._resume_playback_after_scrub = False
        self._video_area.note_activity()

    def _show_image_surface(self) -> None:
        self._video_area.hide_controls(animate=False)
        self._resume_playback_after_scrub = False
        if self._player_stack.currentWidget() is not self._image_viewer:
            self._player_stack.setCurrentWidget(self._image_viewer)

    def _cancel_live_preview(self) -> None:
        self._live_preview_active = False
        self._live_preview_still = None
        self._live_preview_row = None

    def _finish_live_preview(self) -> None:
        if not self._live_preview_active:
            return
        still = self._live_preview_still
        self._live_preview_active = False
        self._live_preview_still = None
        self._live_preview_row = None
        self._media.stop()
        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._show_image_surface()
        self._resume_playback_after_scrub = False
        if still is not None:
            self._status.showMessage(f"Viewing {still.name}")
