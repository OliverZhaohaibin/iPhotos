"""Coordinate playback, preview, and detail view presentation."""

from __future__ import annotations

from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QRect, QTimer
from PySide6.QtWidgets import QStatusBar, QToolButton

from ....config import VIDEO_COMPLETE_HOLD_BACKSTEP_MS
from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel, Roles
from ..widgets.asset_grid import AssetGrid
from ..widgets.player_bar import PlayerBar
from ..widgets.preview_window import PreviewWindow
from .dialog_controller import DialogController
from .header_controller import HeaderController
from .player_view_controller import PlayerViewController
from .view_controller import ViewController
from ..icons import load_icon

if TYPE_CHECKING:  # pragma: no cover - import for type checking only
    from ...facade import AppFacade


class PlayerState(Enum):
    """Describe the high-level presentation state for the playback surface."""

    IDLE = auto()
    TRANSITIONING = auto()
    SHOWING_IMAGE = auto()
    SHOWING_LIVE_STILL = auto()
    PLAYING_LIVE_MOTION = auto()
    PLAYING_VIDEO = auto()
    SHOWING_VIDEO_SURFACE = auto()


class PlaybackController:
    """Encapsulate playback state and UI coordination."""

    def __init__(
        self,
        model: AssetModel,
        media: MediaController,
        playlist: PlaylistController,
        player_bar: PlayerBar,
        grid_view: AssetGrid,
        filmstrip_view: AssetGrid,
        preview_window: PreviewWindow,
        player_view: PlayerViewController,
        view_controller: ViewController,
        header: HeaderController,
        status_bar: QStatusBar,
        dialog: DialogController,
        facade: "AppFacade",
        favorite_button: QToolButton,
    ) -> None:
        self._model = model
        self._media = media
        self._playlist = playlist
        self._player_bar = player_bar
        self._grid_view = grid_view
        self._filmstrip_view = filmstrip_view
        self._preview_window = preview_window
        self._player_view = player_view
        self._view_controller = view_controller
        self._header = header
        self._status = status_bar
        self._dialog = dialog
        self._facade = facade
        self._favorite_button = favorite_button
        self._resume_playback_after_scrub = False
        self._pending_live_photo_still: Path | None = None
        self._original_mute_state = False
        self._active_live_motion: Path | None = None
        self._active_live_still: Path | None = None
        self._state = PlayerState.IDLE
        self._favorite_icon_active = load_icon("suit.heart.fill.svg")
        self._favorite_icon_inactive = load_icon("suit.heart.svg")
        self._current_asset_ref: str | None = None
        self._current_is_favorite = False

        self._favorite_button.clicked.connect(self._toggle_favorite_for_current)
        self._apply_favorite_visual_state(False)
        self._favorite_button.setEnabled(False)

        self._header.clear()
        self._player_view.hide_live_badge()
        self._player_view.set_live_replay_enabled(False)

        media.mutedChanged.connect(self._on_media_muted_changed)
        self._player_view.liveReplayRequested.connect(self.replay_live_photo)
        self._filmstrip_view.nextItemRequested.connect(self._request_next_item)
        self._filmstrip_view.prevItemRequested.connect(self._request_previous_item)
        self._view_controller.galleryViewShown.connect(self._handle_gallery_view_shown)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _set_state(self, new_state: PlayerState) -> None:
        """Update the controller state while guarding redundant transitions.

        Centralising the assignment makes future validation (for example,
        asserting that ``TRANSITIONING`` always leads to a concrete playback
        state) trivial and keeps the intent of each transition obvious to
        readers.
        """

        if self._state == new_state:
            return
        self._state = new_state

    def _is_transitioning(self) -> bool:
        """Return ``True`` while a content transition is in progress."""

        return self._state == PlayerState.TRANSITIONING

    def _is_live_context(self, *, state: PlayerState | None = None) -> bool:
        """Return ``True`` when the supplied or current state is Live Photo specific."""

        target = self._state if state is None else state
        return target in {PlayerState.PLAYING_LIVE_MOTION, PlayerState.SHOWING_LIVE_STILL}

    def _apply_favorite_visual_state(self, is_favorite: bool) -> None:
        """Update the favorite button icon and tooltip to reflect *is_favorite*."""

        icon = self._favorite_icon_active if is_favorite else self._favorite_icon_inactive
        if not icon.isNull():
            self._favorite_button.setIcon(icon)
        tooltip = "Remove from Favorites" if is_favorite else "Mark as Favorite"
        self._favorite_button.setToolTip(tooltip)

    def _update_favorite_button_state(self, row: int | None) -> None:
        """Enable, disable, and retarget the favorite button for *row*."""

        if row is None or row < 0:
            self._current_asset_ref = None
            self._current_is_favorite = False
            self._favorite_button.setEnabled(False)
            self._apply_favorite_visual_state(False)
            return

        index = self._model.index(row, 0)
        if not index.isValid():
            self._current_asset_ref = None
            self._current_is_favorite = False
            self._favorite_button.setEnabled(False)
            self._apply_favorite_visual_state(False)
            return

        rel_raw = index.data(Roles.REL)
        ref = str(rel_raw) if rel_raw else None
        is_favorite = bool(index.data(Roles.FEATURED))
        self._current_asset_ref = ref
        self._current_is_favorite = is_favorite
        self._favorite_button.setEnabled(ref is not None)
        self._apply_favorite_visual_state(is_favorite)

    def _toggle_favorite_for_current(self) -> None:
        """Toggle the featured state for the currently focused asset."""

        if not self._current_asset_ref:
            return

        new_state = self._facade.toggle_featured(self._current_asset_ref)
        self._current_is_favorite = bool(new_state)
        self._apply_favorite_visual_state(self._current_is_favorite)
        # Re-enable the control immediately so the user can toggle back if required.
        self._favorite_button.setEnabled(True)

    # ------------------------------------------------------------------
    # Selection handling
    # ------------------------------------------------------------------
    def activate_index(self, index: QModelIndex) -> None:
        """Handle item activation from either the main grid or the filmstrip."""

        if self._is_transitioning():
            return
        if not index or not index.isValid():
            return

        activating_model = index.model()
        asset_index: QModelIndex | None = None

        # The main gallery grid uses ``self._model`` directly so its indexes
        # can be consumed without translation.
        if activating_model is self._model:
            asset_index = index
        # The filmstrip wraps the asset model with the spacer proxy. Map the
        # proxy index back to the proxy's source (the asset model) before we
        # continue so row calculations remain aligned.
        elif hasattr(activating_model, "mapToSource"):
            mapped = activating_model.mapToSource(index)
            if mapped.isValid():
                asset_index = mapped

        if asset_index is None or not asset_index.isValid():
            return

        row = asset_index.row()
        self._view_controller.show_detail_view()
        self._playlist.set_current(row)

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
        filmstrip_model = self._filmstrip_view.model()
        if filmstrip_model is None:
            return

        self._update_favorite_button_state(row if row >= 0 else None)

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
        if row >= 0:
            _set_is_current(row, True)

        self._header.update_for_row(row if row >= 0 else None, self._model)

        proxy_index: QModelIndex | None = None
        if row >= 0:
            proxy_row = row + 1
            candidate = filmstrip_model.index(proxy_row, 0)
            if candidate.isValid():
                proxy_index = candidate

        # Refresh the spacer width once with the known proxy index so the
        # proxy model can update padding without having to rescan for the
        # current item. This replaces the previous manual geometry updates
        # that forced Qt to recompute the layout repeatedly.
        self._filmstrip_view.refresh_spacers(proxy_index)
        if row < 0:
            self._reset_playback_state(previous_state=self._state, set_idle_state=True)
            self._player_bar.setEnabled(False)
            self._player_view.show_placeholder()
            selection_model.clearSelection()
            return
        selection_model.clearSelection()
        if proxy_index is None:
            return
        selection_model.select(
            proxy_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(
            proxy_index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        # The post-selection refresh keeps the spacer centred when the
        # delegate reports selection-dependent hints (for example, showing a
        # thicker border). The fast path in ``refresh_spacers`` makes this
        # call inexpensive.
        self._filmstrip_view.refresh_spacers(proxy_index)
        QTimer.singleShot(0, lambda idx=proxy_index: self._filmstrip_view.center_on_index(idx))
        self._player_bar.setEnabled(True)
        self._view_controller.show_detail_view()
        self._filmstrip_view.doItemsLayout()

    def handle_playlist_source_changed(self, source: Path) -> None:
        previous_state = self._state
        self._set_state(PlayerState.TRANSITIONING)
        self._reset_playback_state(previous_state=previous_state, set_idle_state=False)
        is_video = False
        is_live_photo = False
        current_row = self._playlist.current_row()
        if current_row != -1:
            index = self._model.index(current_row, 0)
            if index.isValid():
                is_video = bool(index.data(Roles.IS_VIDEO))
                is_live_photo = bool(index.data(Roles.IS_LIVE))
                if is_live_photo:
                    still_raw = index.data(Roles.ABS)
                    if still_raw:
                        still_path = Path(str(still_raw))
                        self._pending_live_photo_still = still_path
                        self._active_live_still = still_path
        self._update_favorite_button_state(current_row if current_row != -1 else None)
        self._header.update_for_row(current_row if current_row != -1 else None, self._model)
        self._preview_window.close_preview(False)

        if not is_video and not is_live_photo:
            self._show_image_asset(source, current_row)
            return

        self._media.load(source)
        if is_live_photo:
            if not self._is_live_context(state=previous_state):
                self._original_mute_state = self._media.is_muted()
            self._active_live_motion = source
            self._media.set_muted(True)
            self._player_view.show_live_badge()
            self._player_view.show_video_surface(interactive=False)
            self._player_bar.setEnabled(False)
            self._player_view.set_live_replay_enabled(False)
        else:
            if self._is_live_context(state=previous_state):
                self._media.set_muted(self._original_mute_state)
            self._player_view.hide_live_badge()
            self._player_view.show_video_surface(interactive=True)
            self._player_bar.setEnabled(True)
            self._player_view.set_live_replay_enabled(False)
        self._view_controller.show_detail_view()
        self._media.play()
        if is_live_photo:
            self._set_state(PlayerState.PLAYING_LIVE_MOTION)
        else:
            self._set_state(PlayerState.PLAYING_VIDEO)
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
            if self._pending_live_photo_still is not None:
                self._show_still_frame_for_live_photo()
            else:
                self._freeze_video_final_frame()
            return
        if name in {"LoadedMedia", "BufferedMedia"}:
            if self._is_transitioning():
                if self._active_live_motion is not None:
                    self._set_state(PlayerState.PLAYING_LIVE_MOTION)
                else:
                    self._set_state(PlayerState.PLAYING_VIDEO)
            self._player_view.note_video_activity()
            return
        if name in {"BufferingMedia", "StalledMedia"}:
            self._player_view.note_video_activity()
            return
        if name in {"InvalidMedia", "NoMedia"}:
            self._reset_playback_state(previous_state=self._state, set_idle_state=True)

    def handle_media_position_changed(self, position_ms: int) -> None:
        self._player_bar.set_position(position_ms)

    # ------------------------------------------------------------------
    # Player bar events
    # ------------------------------------------------------------------
    def toggle_playback(self) -> None:
        if self._player_view.is_showing_video():
            self._player_view.note_video_activity()
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
    def select_filmstrip_row(self, row: int) -> None:
        selection_model = self._filmstrip_view.selectionModel()
        if selection_model is None or row < 0:
            return
        filmstrip_model = self._filmstrip_view.model()
        if filmstrip_model is None:
            return
        proxy_row = row + 1
        proxy_index = filmstrip_model.index(proxy_row, 0)
        if not proxy_index.isValid():
            return
        selection_model.setCurrentIndex(
            proxy_index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        selection_model.select(
            proxy_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._filmstrip_view.refresh_spacers(proxy_index)
        QTimer.singleShot(0, lambda idx=proxy_index: self._filmstrip_view.center_on_index(idx))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _request_next_item(self) -> None:
        if self._is_transitioning():
            return
        self._playlist.next()

    def _request_previous_item(self) -> None:
        if self._is_transitioning():
            return
        self._playlist.previous()

    def _reset_playback_state(
        self,
        previous_state: PlayerState | None = None,
        *,
        set_idle_state: bool = True,
    ) -> None:
        """Stop playback artefacts so the next asset starts from a clean slate.

        Args:
            previous_state: The state that was active before the reset.  Passing the
                prior state allows the controller to restore properties such as the
                original mute flag when leaving a Live Photo context.
            set_idle_state: When ``True`` the controller ends the reset in the
                :class:`PlayerState.IDLE` state.  Callers performing a hand-off to a
                different state can set this to ``False`` and update the state once
                the new presentation has been prepared.
        """

        source_state = self._state if previous_state is None else previous_state
        self._media.stop()
        if self._is_live_context(state=source_state):
            self._media.set_muted(self._original_mute_state)
        self._pending_live_photo_still = None
        self._active_live_motion = None
        self._active_live_still = None
        self._player_view.hide_live_badge()
        self._player_view.set_live_replay_enabled(False)
        self._player_bar.reset()
        self._resume_playback_after_scrub = False
        if set_idle_state:
            self._set_state(PlayerState.IDLE)

    def _handle_gallery_view_shown(self) -> None:
        """Perform cleanup when the UI switches back to the gallery."""

        self._reset_playback_state(previous_state=self._state, set_idle_state=True)
        self._playlist.clear()
        self._player_bar.setEnabled(False)
        self._player_view.show_placeholder()
        self._header.clear()
        self._preview_window.close_preview(False)
        self._filmstrip_view.clearSelection()
        self._grid_view.clearSelection()
        self._status.showMessage("Browse your library")
        self._update_favorite_button_state(None)

    def _show_still_frame_for_live_photo(self) -> None:
        """Swap the detail view back to the Live Photo still image."""

        still_path = self._pending_live_photo_still
        if still_path is None:
            return

        self._pending_live_photo_still = None
        self._active_live_still = still_path

        current_row = self._playlist.current_row()
        self._media.stop()

        if not self._player_view.display_image(still_path):
            self._status.showMessage(f"Unable to display {still_path.name}")
            self._dialog.show_error(f"Could not load {still_path}")
            self._player_view.show_placeholder()
            return

        self._player_view.show_live_badge()
        self._view_controller.show_detail_view()

        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._player_view.set_live_replay_enabled(True)

        if current_row is not None and current_row >= 0:
            self.select_filmstrip_row(current_row)

        self._header.update_for_row(current_row if current_row is not None else None, self._model)
        self._status.showMessage(f"Viewing {still_path.name}")
        self._set_state(PlayerState.SHOWING_LIVE_STILL)

    def _show_image_asset(self, source: Path, row: int | None = None) -> None:
        """Display a still image asset and update related UI state."""

        if not self._player_view.display_image(source):
            self._status.showMessage(f"Unable to display {source.name}")
            self._dialog.show_error(f"Could not load {source}")
            self._player_view.show_placeholder()
            return
        self._pending_live_photo_still = None
        self._active_live_motion = None
        self._active_live_still = None
        self._player_view.hide_live_badge()
        self._player_view.set_live_replay_enabled(False)
        self._preview_window.close_preview(False)
        self._media.stop()
        self._view_controller.show_detail_view()
        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._header.update_for_row(row, self._model)
        self._status.showMessage(f"Viewing {source.name}")
        self._set_state(PlayerState.SHOWING_IMAGE)

    def _freeze_video_final_frame(self) -> None:
        if not self._player_view.is_showing_video():
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
        self._player_view.note_video_activity()
        self._set_state(PlayerState.SHOWING_VIDEO_SURFACE)

    # ------------------------------------------------------------------
    # Live Photo controls
    # ------------------------------------------------------------------
    def replay_live_photo(self) -> None:
        if self._state not in {PlayerState.SHOWING_LIVE_STILL, PlayerState.PLAYING_LIVE_MOTION}:
            return
        if not self._player_view.is_live_badge_visible():
            return
        if not self._player_view.is_showing_image():
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
        self._set_state(PlayerState.TRANSITIONING)
        self._media.stop()
        self._media.load(self._active_live_motion)
        self._player_bar.reset()
        self._player_bar.set_position(0)
        self._player_bar.set_duration(0)
        self._media.set_muted(True)
        self._player_view.show_video_surface(interactive=False)
        self._player_view.set_live_replay_enabled(False)
        self._player_view.show_live_badge()
        self._view_controller.show_detail_view()
        self._media.play()
        self._player_bar.setEnabled(False)
        self._set_state(PlayerState.PLAYING_LIVE_MOTION)
        if still_path is not None:
            self._status.showMessage(f"Playing Live Photo {still_path.name}")
        else:
            self._status.showMessage(f"Playing {self._active_live_motion.name}")

    def _on_media_muted_changed(self, muted: bool) -> None:
        if not self._is_live_context():
            self._original_mute_state = bool(muted)
