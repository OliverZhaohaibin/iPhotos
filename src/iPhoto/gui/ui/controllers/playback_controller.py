"""Coordinate playback, preview, and detail view presentation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QTimer

from ...facade import AppFacade
from ..media import MediaController, PlaylistController
from ..media.media_controller import MediaStatusType
from ..models.asset_model import AssetModel, Roles
from ..widgets.asset_grid import AssetGrid
from .detail_ui_controller import DetailUIController
from .playback_state_manager import PlaybackStateManager, PlayerState
from .preview_controller import PreviewController
from .view_controller import ViewController


class PlaybackController:
    """High-level coordinator that delegates playback tasks to sub-controllers."""

    def __init__(
        self,
        model: AssetModel,
        media: MediaController,
        playlist: PlaylistController,
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
        self._grid_view = grid_view
        self._view_controller = view_controller
        self._detail_ui = detail_ui
        self._state_manager = state_manager
        self._preview_controller = preview_controller
        self._facade = facade
        self._resume_playback_after_scrub = False
        # The timer debounces heavy media loading while the user scrolls
        # quickly.  Parenting it to the filmstrip view keeps its lifetime tied
        # to the UI object tree.
        self._load_delay_timer = QTimer(detail_ui.filmstrip_view)
        self._load_delay_timer.setSingleShot(True)
        self._load_delay_timer.setInterval(10)
        self._load_delay_timer.timeout.connect(self._perform_delayed_load)
        # ``_pending_load_row`` stores the row scheduled for deferred loading.
        # ``-1`` acts as a sentinel indicating that no deferred request exists.
        self._pending_load_row: int = -1

        media.mutedChanged.connect(self._state_manager.handle_media_muted_changed)
        self._state_manager.playbackReset.connect(self._clear_scrub_state)
        self._detail_ui.player_view.liveReplayRequested.connect(self.replay_live_photo)
        self._detail_ui.filmstrip_view.nextItemRequested.connect(self._request_next_item)
        self._detail_ui.filmstrip_view.prevItemRequested.connect(self._request_previous_item)
        viewer = self._detail_ui.player_view.image_viewer
        # Mirror the filmstrip wheel navigation on the main image viewer.
        # This keeps the gesture consistent regardless of which part of the
        # detail view currently has focus.
        viewer.nextItemRequested.connect(self._request_next_item)
        viewer.prevItemRequested.connect(self._request_previous_item)
        self._detail_ui.favorite_button.clicked.connect(self._toggle_favorite)
        self._view_controller.galleryViewShown.connect(self._handle_gallery_view_shown)
        self._detail_ui.scrubbingStarted.connect(self.on_scrub_started)
        self._detail_ui.scrubbingFinished.connect(self.on_scrub_finished)

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

        # Cancel any queued debounced selection because the playlist already
        # committed to a new row, making the pending work obsolete.
        if self._load_delay_timer.isActive():
            self._load_delay_timer.stop()
        self._pending_load_row = -1

        previous_state = self._state_manager.state
        self._state_manager.begin_transition()
        self._media.stop()

        # Postpone the media loading work until the next event loop iteration
        # so quick successive selections keep the UI responsive.
        QTimer.singleShot(0, lambda: self._load_new_source(source, previous_state))

    def _load_new_source(self, source: Path, previous_state: PlayerState) -> None:
        """Carry out the deferred media loading after debouncing completes."""

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
            target_row = current_row if current_row != -1 else None
            self._state_manager.display_image_asset(source, target_row)
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
    def handle_media_status_changed(self, status: MediaStatusType) -> None:
        """Forward media status changes to the state manager."""

        name = getattr(status, "name", None)
        self._state_manager.handle_media_status_changed(status)
        if name in {"EndOfMedia", "InvalidMedia", "NoMedia"}:
            self._clear_scrub_state()

    def toggle_playback(self) -> None:
        """Toggle playback state via the media controller."""

        if self._detail_ui.player_view.is_showing_video():
            self._detail_ui.player_view.note_video_activity()
        state = self._media.playback_state()
        playing = getattr(state, "name", None) == "PlayingState"
        if not playing:
            if self._detail_ui.is_player_at_end():
                self._media.seek(0)
                self._detail_ui.set_player_position_to_start()
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
        """Advance to the next playlist item with debounce handling."""

        if self._state_manager.is_transitioning() and not self._load_delay_timer.isActive():
            return
        self._handle_navigation_request(1)

    def _request_previous_item(self) -> None:
        """Select the previous playlist item with debounce handling."""

        if self._state_manager.is_transitioning() and not self._load_delay_timer.isActive():
            return
        self._handle_navigation_request(-1)

    def _handle_navigation_request(self, delta: int) -> None:
        """Queue a navigation request and delay loading until scrolling pauses."""

        target_row = self._playlist.peek_next_row(delta)
        if target_row is None:
            return
        # Update the playlist selection immediately so the UI highlights track
        # user intent, but defer loading until the timer fires.
        self._playlist.set_current_row_only(target_row)
        self._pending_load_row = target_row
        self._load_delay_timer.start()

    def _perform_delayed_load(self) -> None:
        """Commit the deferred selection once the debounce timer expires."""

        if self._pending_load_row == -1:
            return
        self._playlist.set_current(self._pending_load_row)
        self._pending_load_row = -1

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
