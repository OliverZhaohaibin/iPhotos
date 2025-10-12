"""Coordinate playback, preview, and detail view presentation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex

from ...facade import AppFacade
from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel, Roles
from ..widgets.asset_grid import AssetGrid
from ..widgets.player_bar import PlayerBar
from .detail_ui_controller import DetailUIController
from .playback_state_manager import PlaybackStateManager
from .preview_controller import PreviewController
from .view_controller import ViewController


class PlaybackController:
    """High-level coordinator that delegates playback tasks to sub-controllers."""

    def __init__(
        self,
        model: AssetModel,
        media: MediaController,
        playlist: PlaylistController,
        player_bar: PlayerBar,
        grid_view: AssetGrid,
        view_controller: ViewController,
        detail_ui: DetailUIController,
        state_manager: PlaybackStateManager,
        preview_controller: PreviewController,
        facade: AppFacade,
    ) -> None:
        """Store dependencies and wire cross-controller signals."""

        self._model = model
        self._media = media
        self._playlist = playlist
        self._player_bar = player_bar
        self._grid_view = grid_view
        self._view_controller = view_controller
        self._detail_ui = detail_ui
        self._state_manager = state_manager
        self._preview_controller = preview_controller
        self._facade = facade
        self._resume_playback_after_scrub = False

        media.mutedChanged.connect(self._state_manager.handle_media_muted_changed)
        self._state_manager.playbackReset.connect(self._clear_scrub_state)
        self._detail_ui.player_view.liveReplayRequested.connect(self.replay_live_photo)
        self._detail_ui.filmstrip_view.nextItemRequested.connect(self._request_next_item)
        self._detail_ui.filmstrip_view.prevItemRequested.connect(self._request_previous_item)
        self._detail_ui.favorite_button.clicked.connect(self._toggle_favorite)
        self._view_controller.galleryViewShown.connect(self._handle_gallery_view_shown)

    # ------------------------------------------------------------------
    # Selection handling
    # ------------------------------------------------------------------
    def activate_index(self, index: QModelIndex) -> None:
        """Handle item activation from either the main grid or the filmstrip."""

        if self._state_manager.is_transitioning():
            return
        if not index or not index.isValid():
            return

        activating_model = index.model()
        asset_index: QModelIndex | None = None

        if activating_model is self._model:
            asset_index = index
        elif hasattr(activating_model, "mapToSource"):
            mapped = activating_model.mapToSource(index)
            if mapped.isValid():
                asset_index = mapped

        if asset_index is None or not asset_index.isValid():
            return

        row = asset_index.row()
        self._view_controller.show_detail_view()
        self._playlist.set_current(row)

    # ------------------------------------------------------------------
    # Playlist callbacks
    # ------------------------------------------------------------------
    def handle_playlist_current_changed(self, row: int) -> None:
        """Update UI state to reflect the playlist's current row."""

        previous_row = self._playlist.previous_row()
        self._detail_ui.handle_playlist_current_changed(row, previous_row)
        if row < 0:
            self._state_manager.reset(previous_state=self._state_manager.state, set_idle_state=True)
            self._clear_scrub_state()

    def handle_playlist_source_changed(self, source: Path) -> None:
        """Load and present the media source associated with the current row."""

        previous_state = self._state_manager.state
        self._state_manager.begin_transition()
        self._state_manager.reset(previous_state=previous_state, set_idle_state=False)
        self._clear_scrub_state()

        current_row = self._playlist.current_row()
        self._detail_ui.update_favorite_button(current_row)
        self._detail_ui.update_header(current_row if current_row != -1 else None)
        self._preview_controller.close_preview(False)

        is_video = False
        is_live_photo = False
        still_path: Path | None = None
        if current_row != -1:
            index = self._model.index(current_row, 0)
            if index.isValid():
                is_video = bool(index.data(Roles.IS_VIDEO))
                is_live_photo = bool(index.data(Roles.IS_LIVE))
                if is_live_photo:
                    still_raw = index.data(Roles.ABS)
                    if still_raw:
                        still_path = Path(str(still_raw))

        if not is_video and not is_live_photo:
            self._state_manager.display_image_asset(source, current_row if current_row != -1 else None)
            self._clear_scrub_state()
            return

        self._state_manager.start_media_playback(
            source,
            is_live_photo=is_live_photo,
            still_path=still_path,
            previous_state=previous_state,
        )

    # ------------------------------------------------------------------
    # Media callbacks
    # ------------------------------------------------------------------
    def handle_media_status_changed(self, status: object) -> None:
        """Forward media status changes to the state manager."""

        name = getattr(status, "name", None)
        self._state_manager.handle_media_status_changed(status)
        if name in {"EndOfMedia", "InvalidMedia", "NoMedia"}:
            self._clear_scrub_state()

    def handle_media_position_changed(self, position_ms: int) -> None:
        """Keep the player bar in sync with the media engine."""

        self._player_bar.set_position(position_ms)

    # ------------------------------------------------------------------
    # Player bar events
    # ------------------------------------------------------------------
    def toggle_playback(self) -> None:
        """Toggle playback state via the media controller."""

        if self._detail_ui.player_view.is_showing_video():
            self._detail_ui.player_view.note_video_activity()
        state = self._media.playback_state()
        playing = getattr(state, "name", None) == "PlayingState"
        if not playing:
            duration = self._player_bar.duration()
            if duration > 0 and self._player_bar.position() >= duration:
                self._media.seek(0)
                self._player_bar.set_position(0)
        self._media.toggle()

    def on_scrub_started(self) -> None:
        """Pause playback while the user scrubs the timeline."""

        state = self._media.playback_state()
        self._resume_playback_after_scrub = getattr(state, "name", "") == "PlayingState"
        if self._resume_playback_after_scrub:
            self._media.pause()

    def on_scrub_finished(self) -> None:
        """Resume playback after scrubbing if playback was active."""

        if self._resume_playback_after_scrub:
            self._media.play()
        self._resume_playback_after_scrub = False

    # ------------------------------------------------------------------
    # Favorite controls
    # ------------------------------------------------------------------
    def _toggle_favorite(self) -> None:
        """Toggle the featured flag for the playlist's current asset."""

        current_row = self._playlist.current_row()
        if current_row == -1:
            return

        index = self._model.index(current_row, 0)
        if not index.isValid():
            return

        rel = str(index.data(Roles.REL) or "")
        if not rel:
            return

        is_featured = self._facade.toggle_featured(rel)
        self._detail_ui.update_favorite_button(current_row, is_featured=is_featured)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _request_next_item(self) -> None:
        """Advance to the next playlist item when not transitioning."""

        if self._state_manager.is_transitioning():
            return
        self._playlist.next()

    def _request_previous_item(self) -> None:
        """Select the previous playlist item when not transitioning."""

        if self._state_manager.is_transitioning():
            return
        self._playlist.previous()

    def _handle_gallery_view_shown(self) -> None:
        """Perform cleanup when the UI switches back to the gallery."""

        self._state_manager.reset(previous_state=self._state_manager.state, set_idle_state=True)
        self._clear_scrub_state()
        self._playlist.clear()
        self._detail_ui.reset_for_gallery_view()
        self._preview_controller.close_preview(False)
        self._grid_view.clearSelection()

    def replay_live_photo(self) -> None:
        """Request the state manager to replay the active Live Photo."""

        self._preview_controller.close_preview(False)
        self._state_manager.replay_live_photo()

    def _clear_scrub_state(self) -> None:
        """Ensure scrub-related state does not leak across transitions."""

        self._resume_playback_after_scrub = False
