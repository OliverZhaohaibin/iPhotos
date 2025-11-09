"""Coordinate playback, preview, and detail view presentation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QTimer

from ...facade import AppFacade
from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel, Roles
from ..widgets.asset_grid import AssetGrid
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
        self._detail_ui.scrubbingStarted.connect(self.on_scrub_started)
        self._detail_ui.scrubbingFinished.connect(self.on_scrub_finished)

    # ------------------------------------------------------------------
    # Selection handling
    # ------------------------------------------------------------------
    def stop_and_unload(
        self,
        *,
        previous_state: object | None = None,
        set_idle_state: bool,
    ) -> None:
        """Synchronously stop playback and discard any loaded media source.

        Parameters
        ----------
        previous_state:
            Optional hint describing the playback state that initiated the
            reset.  Providing the original state keeps Live Photo mute
            bookkeeping aligned with the legacy controller.
        set_idle_state:
            When ``True`` the playback state manager is forced back to the
            idle state once cleanup completes.
        """

        if self._load_delay_timer.isActive():
            self._load_delay_timer.stop()
        self._pending_load_row = -1

        state_hint = previous_state if previous_state is not None else self._state_manager.state

        # Resetting through the state manager stops playback, clears Live Photo
        # affordances, and emits ``playbackReset`` so scrub state is purged.
        self._state_manager.reset(previous_state=state_hint, set_idle_state=set_idle_state)
        self._detail_ui.set_player_bar_enabled(False)

        # Drop the current media source so Qt releases any file handles before
        # loading the next asset.
        self._media.unload()

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
        if row < 0:
            self.stop_and_unload(previous_state=self._state_manager.state, set_idle_state=True)
        self._detail_ui.handle_playlist_current_changed(row, previous_row)

    def handle_playlist_source_changed(self, source: Path) -> None:
        """Load and present the media source associated with the current row."""

        # Cancel any queued debounced selection because the playlist already
        # committed to a new row, making the pending work obsolete.
        if self._load_delay_timer.isActive():
            self._load_delay_timer.stop()
        self._pending_load_row = -1

        previous_state = self._state_manager.state
        self._state_manager.begin_transition()

        current_row = self._playlist.current_row()
        if current_row == -1:
            self.stop_and_unload(previous_state=previous_state, set_idle_state=True)
            self._preview_controller.close_preview(False)
            self._detail_ui.show_placeholder()
            return

        # Abort media work if the detail view is no longer visible.  The reset
        # keeps the backend idle so a later activation starts from a clean
        # slate.
        if not self._view_controller.is_detail_view_active():
            self.stop_and_unload(previous_state=previous_state, set_idle_state=True)
            return

        self.stop_and_unload(previous_state=previous_state, set_idle_state=False)

        self._detail_ui.update_favorite_button(current_row)
        self._detail_ui.update_header(current_row if current_row != -1 else None)
        self._preview_controller.close_preview(False)

        index = self._model.index(current_row, 0)
        self.load_asset(index, fallback_source=source, previous_state=previous_state)

    def load_asset(
        self,
        index: QModelIndex,
        *,
        fallback_source: Path | None = None,
        previous_state: object | None = None,
    ) -> None:
        """Load *index* into the player, handling images, videos and Live Photos."""

        if not index.isValid():
            self._detail_ui.show_placeholder()
            self._state_manager.reset(previous_state=self._state_manager.state, set_idle_state=True)
            return

        def _coerce_path(value: object) -> Path | None:
            if isinstance(value, Path):
                return value
            if isinstance(value, str) and value:
                return Path(value)
            return None

        media_state = previous_state if previous_state is not None else self._state_manager.state
        is_live_photo = bool(index.data(Roles.IS_LIVE))
        is_video = bool(index.data(Roles.IS_VIDEO))
        still_path = _coerce_path(index.data(Roles.ABS))

        if is_live_photo:
            motion_path = _coerce_path(index.data(Roles.LIVE_MOTION_ABS))
            if motion_path is None:
                rel_motion = index.data(Roles.LIVE_MOTION_REL)
                if isinstance(rel_motion, str) and rel_motion:
                    album_root = self._model.source_model().album_root()
                    if album_root is not None:
                        motion_path = (album_root / rel_motion).resolve()
            if motion_path is None and fallback_source is not None:
                motion_path = fallback_source
            if motion_path is not None:
                self._state_manager.start_media_playback(
                    motion_path,
                    is_live_photo=True,
                    still_path=still_path,
                    previous_state=media_state,
                )
                return
            # Fall back to the still frame if the paired motion clip is missing.
            is_live_photo = False

        if is_video and not is_live_photo:
            video_path = _coerce_path(index.data(Roles.ABS)) or fallback_source
            if video_path is not None:
                self._state_manager.start_media_playback(
                    video_path,
                    is_live_photo=False,
                    still_path=None,
                    previous_state=media_state,
                )
                return

        image_path = still_path or fallback_source
        if image_path is not None:
            self._state_manager.display_image_asset(image_path, index.row())
            return

        # Reaching this point means the model did not provide any usable path.
        self._detail_ui.show_status_message("Unable to load the selected item")
        self._detail_ui.show_placeholder()
        self._state_manager.reset(previous_state=self._state_manager.state, set_idle_state=True)

    # ------------------------------------------------------------------
    # Media callbacks
    # ------------------------------------------------------------------
    def handle_media_status_changed(self, status: object) -> None:
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

    def request_next_item(self) -> None:
        """Advance to the next playlist entry without duplicating debounce logic."""

        # The internal ``_request_next_item`` helper already guards against rapid
        # navigation events by checking the playback state manager.  Exposing a
        # thin wrapper keeps keyboard shortcuts and UI affordances aligned while
        # centralising the debouncing behaviour in a single location.
        self._request_next_item()

    def request_previous_item(self) -> None:
        """Navigate to the previous playlist entry while honouring debounce rules."""

        # See :meth:`request_next_item` for the rationale behind the public
        # wrapper.  The playback controller remains the single source of truth
        # for playlist navigation, ensuring keyboard shortcuts cannot bypass the
        # transition guards that keep the UI responsive.
        self._request_previous_item()

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

    def reset_for_gallery_navigation(self) -> None:
        """Clear playback state before the UI returns to a gallery-style view.

        The detail pane and playlist remain active while the user inspects
        individual assets.  Navigating back to any gallery (all photos, albums,
        static collections, etc.) should leave the playback widgets in a clean
        state so the grid does not momentarily show stale selections or continue
        streaming media.  This helper mirrors the logic that previously ran in
        response to ``ViewController.galleryViewShown`` but exposes it as an
        explicit call.  Callers can therefore reset the playback layer exactly
        once during navigation, preventing the redundant model resets that were
        responsible for tab flicker.
        """

        self.stop_and_unload(previous_state=self._state_manager.state, set_idle_state=True)
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
