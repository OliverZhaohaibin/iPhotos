"""Coordinate playback, preview, and detail view presentation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QRect
from PySide6.QtWidgets import QAbstractItemView, QStackedWidget, QStatusBar, QWidget

from ....config import VIDEO_COMPLETE_HOLD_BACKSTEP_MS
from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel, Roles
from ...utils import image_loader
from ..widgets.asset_grid import AssetGrid
from ..widgets.image_viewer import ImageViewer
from ..widgets.player_bar import PlayerBar
from ..widgets.video_area import VideoArea
from ..widgets.preview_window import PreviewWindow
from ..widgets.live_badge import LiveBadge
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
        live_badge: LiveBadge,
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
        self._live_badge = live_badge
        self._status = status_bar
        self._dialog = dialog
        self._resume_playback_after_scrub = False
        self._pending_live_photo_still: Path | None = None
        self._original_mute_state = False
        self._live_mode_active = False
        self._active_live_motion: Path | None = None
        self._active_live_still: Path | None = None
        self._is_transitioning = False
        media.mutedChanged.connect(self._on_media_muted_changed)
        self._image_viewer.replayRequested.connect(self.replay_live_photo)
        self._image_viewer.set_live_replay_enabled(False)
        self._filmstrip_view.nextItemRequested.connect(self._request_next_item)
        self._filmstrip_view.prevItemRequested.connect(self._request_previous_item)

    # ------------------------------------------------------------------
    # Selection handling
    # ------------------------------------------------------------------
    def activate_index(self, index: QModelIndex) -> None:
        if self._is_transitioning:
            return
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
            self.show_detail_view()
            self._playlist.set_current(row)
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

        previous_row = self._playlist.previous_row()
        _set_is_current(previous_row, False)
        if row < 0:
            self._player_bar.reset()
            self._player_bar.setEnabled(False)
            self._media.stop()
            self._show_player_placeholder()
            selection_model.clearSelection()
            self._release_transition_lock()
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
        _set_is_current(row, True)
        self._filmstrip_view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        self._player_bar.setEnabled(True)
        self.show_detail_view()

    def handle_playlist_source_changed(self, source: Path) -> None:
        self._is_transitioning = True
        self._reset_playback_state()
        is_live_photo = False
        current_row = self._playlist.current_row()
        if current_row != -1:
            index = self._model.index(current_row, 0)
            if index.isValid():
                is_live_photo = bool(index.data(Roles.IS_LIVE))
                if is_live_photo:
                    still_raw = index.data(Roles.ABS)
                    if still_raw:
                        still_path = Path(str(still_raw))
                        self._pending_live_photo_still = still_path
                        self._active_live_still = still_path
        self._preview_window.close_preview(False)
        self._media.load(source)
        if is_live_photo:
            if not self._live_mode_active:
                self._original_mute_state = self._media.is_muted()
            self._live_mode_active = True
            self._active_live_motion = source
            self._media.set_muted(True)
            self._live_badge.show()
            self._live_badge.raise_()
            self._show_video_surface(interactive=False)
        else:
            if self._live_mode_active:
                self._media.set_muted(self._original_mute_state)
            self._live_mode_active = False
            self._show_video_surface(interactive=True)
        self.show_detail_view()
        self._media.play()
        if is_live_photo and self._active_live_still is not None:
            self._status.showMessage(
                f"Playing Live Photo {self._active_live_still.name}"
            )
        else:
            self._status.showMessage(f"Playing {source.name}")

    # ------------------------------------------------------------------
    # Media callbacks
    # ------------------------------------------------------------------
    def handle_media_status_changed(self, status: object) -> None:
        name = getattr(status, "name", None)
        if name == "EndOfMedia":
            self._release_transition_lock()
            if self._pending_live_photo_still is not None:
                self._show_still_frame_for_live_photo()
            else:
                self._freeze_video_final_frame()
            return
        if name in {"LoadedMedia", "BufferedMedia"}:
            self._release_transition_lock()
            self._video_area.note_activity()
            return
        if name in {"BufferingMedia", "StalledMedia"}:
            self._video_area.note_activity()
            return
        if name in {"InvalidMedia", "NoMedia"}:
            self._release_transition_lock()

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
        self._release_transition_lock()
        self._pending_live_photo_still = None
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
    def _request_next_item(self) -> None:
        if self._is_transitioning:
            return
        self._playlist.next()

    def _request_previous_item(self) -> None:
        if self._is_transitioning:
            return
        self._playlist.previous()

    def _reset_playback_state(self) -> None:
        self._media.stop()
        if self._live_mode_active:
            self._media.set_muted(self._original_mute_state)
        self._pending_live_photo_still = None
        self._active_live_motion = None
        self._active_live_still = None
        self._live_mode_active = False
        self._live_badge.hide()
        self._image_viewer.set_live_replay_enabled(False)
        self._player_bar.reset()
        self._resume_playback_after_scrub = False

    def _release_transition_lock(self) -> None:
        self._is_transitioning = False

    def _show_still_frame_for_live_photo(self) -> None:
        """Swap the detail view back to the Live Photo still image."""
        still_path = self._pending_live_photo_still
        if still_path is None:
            return

        self._pending_live_photo_still = None
        self._live_mode_active = True
        self._active_live_still = still_path

        current_row = self._playlist.current_row()
        self._media.stop()

        pixmap = image_loader.load_qpixmap(still_path)
        if pixmap is None:
            self._status.showMessage(f"Unable to display {still_path.name}")
            self._dialog.show_error(f"Could not load {still_path}")
            self._show_player_placeholder()
            return

        self._image_viewer.set_pixmap(pixmap)
        self._live_badge.show()
        self._live_badge.raise_()
        self._show_image_surface()
        self.show_detail_view()

        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._image_viewer.set_live_replay_enabled(True)

        if current_row is not None and current_row >= 0:
            self.select_filmstrip_row(current_row)

        self._status.showMessage(f"Viewing {still_path.name}")

    def _display_image(self, source: Path, row: int | None = None) -> None:
        pixmap = image_loader.load_qpixmap(source)
        if pixmap is None:
            self._status.showMessage(f"Unable to display {source.name}")
            self._dialog.show_error(f"Could not load {source}")
            return
        self._pending_live_photo_still = None
        if self._live_mode_active:
            self._media.set_muted(self._original_mute_state)
        self._live_mode_active = False
        self._active_live_motion = None
        self._active_live_still = None
        self._live_badge.hide()
        self._image_viewer.set_live_replay_enabled(False)
        self._preview_window.close_preview(False)
        self._media.stop()
        self._image_viewer.set_pixmap(pixmap)
        self._show_image_surface()
        self.show_detail_view()
        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        if row is not None:
            self.select_filmstrip_row(row)
        self._status.showMessage(f"Viewing {source.name}")

    def _show_player_placeholder(self) -> None:
        self._pending_live_photo_still = None
        if self._live_mode_active:
            self._media.set_muted(self._original_mute_state)
        self._live_mode_active = False
        self._active_live_motion = None
        self._active_live_still = None
        self._live_badge.hide()
        self._image_viewer.set_live_replay_enabled(False)
        self._video_area.hide_controls(animate=False)
        self._resume_playback_after_scrub = False
        if self._player_stack.currentWidget() is not self._player_placeholder:
            self._player_stack.setCurrentWidget(self._player_placeholder)
        self._image_viewer.clear()

    def _show_video_surface(self, *, interactive: bool = True) -> None:
        if self._player_stack.currentWidget() is not self._video_area:
            self._player_stack.setCurrentWidget(self._video_area)
        if not self._player_stack.isVisible():
            self._player_stack.show()
        self._resume_playback_after_scrub = False
        self._video_area.set_controls_enabled(interactive)
        if interactive:
            self._player_bar.setEnabled(True)
            self._video_area.show_controls(animate=False)
        else:
            self._player_bar.setEnabled(False)
            self._video_area.hide_controls(animate=False)

    def _freeze_video_final_frame(self) -> None:
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
        if not self._player_stack.isVisible():
            self._player_stack.show()

    # ------------------------------------------------------------------
    # Live Photo controls
    # ------------------------------------------------------------------
    def replay_live_photo(self) -> None:
        if not self._live_mode_active:
            return
        if not self._live_badge.isVisible():
            return
        if self._player_stack.currentWidget() is not self._image_viewer:
            return
        motion_source = self._active_live_motion or self._playlist.current_source()
        if motion_source is None:
            return
        still_path = self._active_live_still
        if still_path is None:
            current_row = self._playlist.current_row()
            if current_row != -1:
                index = self._model.index(current_row, 0)
                if index.isValid():
                    still_raw = index.data(Roles.ABS)
                    if still_raw:
                        still_path = Path(str(still_raw))
        if still_path is not None:
            self._pending_live_photo_still = still_path
            self._active_live_still = still_path
        self._active_live_motion = Path(motion_source)
        self._preview_window.close_preview(False)
        self._live_mode_active = True
        self._media.stop()
        self._media.load(self._active_live_motion)
        self._player_bar.reset()
        self._player_bar.set_position(0)
        self._player_bar.set_duration(0)
        self._media.set_muted(True)
        self._show_video_surface(interactive=False)
        self._image_viewer.set_live_replay_enabled(False)
        self._live_badge.show()
        self._live_badge.raise_()
        self.show_detail_view()
        self._media.play()
        if still_path is not None:
            self._status.showMessage(f"Playing Live Photo {still_path.name}")
        else:
            self._status.showMessage(f"Playing {self._active_live_motion.name}")

    def _on_media_muted_changed(self, muted: bool) -> None:
        if not self._live_mode_active:
            self._original_mute_state = bool(muted)

