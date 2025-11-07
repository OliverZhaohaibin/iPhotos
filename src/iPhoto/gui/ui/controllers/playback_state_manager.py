"""Maintain the playback state machine used by :class:`PlaybackController`."""

from __future__ import annotations

from enum import Enum, auto
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ....config import VIDEO_COMPLETE_HOLD_BACKSTEP_MS
from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel, Roles
from .detail_ui_controller import DetailUIController
from .dialog_controller import DialogController


class PlayerState(Enum):
    """Describe the high-level presentation state for the playback surface."""

    IDLE = auto()
    TRANSITIONING = auto()
    SHOWING_IMAGE = auto()
    SHOWING_LIVE_STILL = auto()
    PLAYING_LIVE_MOTION = auto()
    PLAYING_VIDEO = auto()
    SHOWING_VIDEO_SURFACE = auto()


class PlaybackStateManager(QObject):
    """Encapsulate state transitions for playback and Live Photo contexts."""

    playbackReset = Signal()
    """Emitted whenever :meth:`reset` completes."""

    def __init__(
        self,
        media: MediaController,
        playlist: PlaylistController,
        model: AssetModel,
        detail_ui: DetailUIController,
        dialog: DialogController,
        parent: Optional[QObject] = None,
    ) -> None:
        """Store dependencies required to coordinate playback state."""

        super().__init__(parent)
        self._media = media
        self._playlist = playlist
        self._model = model
        self._detail_ui = detail_ui
        self._dialog = dialog
        self._state = PlayerState.IDLE
        self._pending_live_photo_still: Optional[Path] = None
        self._original_mute_state = False
        self._active_live_motion: Optional[Path] = None
        self._active_live_still: Optional[Path] = None
        self._detail_ui.player_view.imageLoadingFailed.connect(
            self._on_image_loading_failed
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def state(self) -> PlayerState:
        """Return the currently active state."""

        return self._state

    def begin_transition(self) -> None:
        """Mark that a state transition is under way."""

        self._set_state(PlayerState.TRANSITIONING)

    def is_transitioning(self) -> bool:
        """Return ``True`` while the controller is in the transition state."""

        return self._state == PlayerState.TRANSITIONING

    def is_live_context(self, *, state: Optional[PlayerState] = None) -> bool:
        """Return ``True`` if *state* represents a Live Photo surface."""

        target = self._state if state is None else state
        return target in {PlayerState.PLAYING_LIVE_MOTION, PlayerState.SHOWING_LIVE_STILL}

    def reset(self, previous_state: Optional[PlayerState] = None, *, set_idle_state: bool) -> None:
        """Stop playback artefacts so the next asset starts from a clean slate."""

        source_state = self._state if previous_state is None else previous_state
        self._media.stop()
        if self.is_live_context(state=source_state):
            self._media.set_muted(self._original_mute_state)
        self._pending_live_photo_still = None
        self._active_live_motion = None
        self._active_live_still = None
        self._detail_ui.hide_live_badge()
        self._detail_ui.set_live_replay_enabled(False)
        self._detail_ui.reset_player_bar()
        if set_idle_state:
            self._set_state(PlayerState.IDLE)
        self.playbackReset.emit()

    def display_image_asset(self, source: Path, row: Optional[int]) -> None:
        """Display an image asset on the player surface."""

        if not self._detail_ui.player_view.display_image(source):
            self._detail_ui.show_status_message(f"Unable to display {source.name}")
            self._dialog.show_error(f"Could not load {source}")
            self._detail_ui.show_placeholder()
            return
        self._pending_live_photo_still = None
        self._active_live_motion = None
        self._active_live_still = None
        self._media.stop()
        self._detail_ui.hide_live_badge()
        self._detail_ui.show_detail_view()
        self._detail_ui.show_zoom_controls()
        self._detail_ui.reset_player_bar()
        self._detail_ui.set_player_bar_enabled(False)
        self._detail_ui.update_header(row if row is not None else None)
        self._detail_ui.show_status_message(f"Viewing {source.name}")
        self._set_state(PlayerState.SHOWING_IMAGE)

    def start_media_playback(
        self,
        source: Path,
        *,
        is_live_photo: bool,
        still_path: Optional[Path],
        previous_state: PlayerState,
    ) -> None:
        """Prepare and start playback for a video or Live Photo motion clip."""

        self._media.load(source)
        if is_live_photo:
            if not self.is_live_context(state=previous_state):
                self._original_mute_state = self._media.is_muted()
            self._active_live_motion = source
            self._pending_live_photo_still = still_path
            if still_path is not None:
                self._active_live_still = still_path
            self._media.set_muted(True)
            self._detail_ui.show_live_badge()
            self._detail_ui.player_view.show_video_surface(interactive=False)
            self._detail_ui.set_player_bar_enabled(False)
            self._detail_ui.player_view.set_live_replay_enabled(False)
            self._detail_ui.hide_zoom_controls()
        else:
            if self.is_live_context(state=previous_state):
                self._media.set_muted(self._original_mute_state)
            self._detail_ui.hide_live_badge()
            self._detail_ui.player_view.show_video_surface(interactive=True)
            self._detail_ui.set_player_bar_enabled(True)
            self._detail_ui.set_live_replay_enabled(False)
            self._pending_live_photo_still = None
            self._active_live_motion = None
            self._active_live_still = None
            self._detail_ui.hide_zoom_controls()
        self._detail_ui.show_detail_view()
        self._media.play()
        if is_live_photo:
            self._set_state(PlayerState.PLAYING_LIVE_MOTION)
            label = (
                f"Playing Live Photo {self._active_live_still.name}"
                if self._active_live_still is not None
                else f"Playing {source.name}"
            )
        else:
            self._set_state(PlayerState.PLAYING_VIDEO)
            label = f"Playing {source.name}"
        self._detail_ui.show_status_message(label)

    def handle_media_status_changed(self, status: object) -> None:
        """React to status changes emitted by the media backend."""

        name = getattr(status, "name", None)
        if name == "EndOfMedia":
            if self._pending_live_photo_still is not None:
                self._show_still_frame_for_live_photo()
            else:
                self._freeze_video_final_frame()
            return
        if name in {"LoadedMedia", "BufferedMedia"}:
            if self.is_transitioning():
                if self._active_live_motion is not None:
                    self._set_state(PlayerState.PLAYING_LIVE_MOTION)
                else:
                    self._set_state(PlayerState.PLAYING_VIDEO)
            self._detail_ui.player_view.note_video_activity()
            return
        if name in {"BufferingMedia", "StalledMedia"}:
            self._detail_ui.player_view.note_video_activity()
            return
        if name in {"InvalidMedia", "NoMedia"}:
            self.reset(previous_state=self._state, set_idle_state=True)

    def handle_media_muted_changed(self, muted: bool) -> None:
        """Track mute changes so Live Photo transitions can restore state."""

        if not self.is_live_context():
            self._original_mute_state = bool(muted)

    # ------------------------------------------------------------------
    # Worker callbacks
    # ------------------------------------------------------------------
    def _on_image_loading_failed(self, source: Path, message: str) -> None:
        """Handle asynchronous image loading failures gracefully."""

        current = self._playlist.current_source()
        if current is None or current != source:
            return

        self._detail_ui.show_status_message(f"Unable to display {source.name}")
        self._dialog.show_error(f"Could not load {source}: {message}")
        self._detail_ui.show_placeholder()
        self._set_state(PlayerState.IDLE)

    def replay_live_photo(self) -> None:
        """Replay the motion clip for the currently displayed Live Photo."""

        if self._state not in {PlayerState.SHOWING_LIVE_STILL, PlayerState.PLAYING_LIVE_MOTION}:
            return
        if not self._detail_ui.player_view.is_live_badge_visible():
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
        self.begin_transition()
        self._media.stop()
        self._media.load(self._active_live_motion)
        self._detail_ui.reset_player_bar()
        self._detail_ui.set_player_position_to_start()
        self._detail_ui.set_player_duration(0)
        self._media.set_muted(True)
        self._detail_ui.player_view.show_video_surface(interactive=False)
        self._detail_ui.player_view.set_live_replay_enabled(False)
        self._detail_ui.show_live_badge()
        self._detail_ui.show_detail_view()
        self._media.play()
        self._detail_ui.set_player_bar_enabled(False)
        self._set_state(PlayerState.PLAYING_LIVE_MOTION)
        if still_path is not None:
            self._detail_ui.show_status_message(f"Playing Live Photo {still_path.name}")
        else:
            self._detail_ui.show_status_message(f"Playing {self._active_live_motion.name}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _set_state(self, new_state: PlayerState) -> None:
        if self._state == new_state:
            return
        self._state = new_state

    def _show_still_frame_for_live_photo(self) -> None:
        still_path = self._pending_live_photo_still
        if still_path is None:
            return
        self._pending_live_photo_still = None
        self._active_live_still = still_path
        self._media.stop()
        if not self._detail_ui.player_view.display_image(still_path):
            self._detail_ui.show_status_message(f"Unable to display {still_path.name}")
            self._dialog.show_error(f"Could not load {still_path}")
            self._detail_ui.show_placeholder()
            return
        self._detail_ui.show_live_badge()
        self._detail_ui.show_detail_view()
        self._detail_ui.reset_player_bar()
        self._detail_ui.set_player_bar_enabled(False)
        self._detail_ui.player_view.set_live_replay_enabled(True)
        self._detail_ui.show_zoom_controls()

        current_row = self._playlist.current_row()
        if current_row is not None and current_row >= 0:
            self._detail_ui.select_filmstrip_row(current_row)
        self._detail_ui.update_header(current_row if current_row is not None else None)
        self._detail_ui.show_status_message(f"Viewing {still_path.name}")
        self._set_state(PlayerState.SHOWING_LIVE_STILL)

    def _freeze_video_final_frame(self) -> None:
        if not self._detail_ui.player_view.is_showing_video():
            return
        duration = self._detail_ui.player_duration()
        if duration <= 0:
            return
        backstep = max(0, VIDEO_COMPLETE_HOLD_BACKSTEP_MS)
        target = max(0, duration - backstep)
        self._media.seek(target)
        self._media.pause()
        self._detail_ui.set_player_position(duration)
        self._detail_ui.player_view.note_video_activity()
        self._set_state(PlayerState.SHOWING_VIDEO_SURFACE)
