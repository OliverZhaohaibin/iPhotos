"""Coordinator that wires the main window to application logic."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtCore import QModelIndex

# ``main_controller`` shares the same import caveat as ``main_window``.  The
# fallback ensures running the module as a script still locates ``AppContext``.
try:  # pragma: no cover - exercised in packaging scenarios
    from ....appctx import AppContext
except ImportError:  # pragma: no cover - script execution fallback
    from iPhotos.src.iPhoto.appctx import AppContext

from ...facade import AppFacade
from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel, Roles
from ..models.spacer_proxy_model import SpacerProxyModel
from ..widgets import AssetGridDelegate
from .detail_ui_controller import DetailUIController
from .dialog_controller import DialogController
from .header_controller import HeaderController
from .navigation_controller import NavigationController
from .playback_controller import PlaybackController
from .playback_state_manager import PlaybackStateManager
from .player_view_controller import PlayerViewController
from .preview_controller import PreviewController
from .status_bar_controller import StatusBarController
from .view_controller import ViewController

if TYPE_CHECKING:
    from ..main_window import MainWindow


class MainController(QObject):
    """High-level coordinator for the main window."""

    def __init__(self, window: "MainWindow", context: AppContext) -> None:
        super().__init__(window)
        self._window = window
        self._context = context
        self._facade: AppFacade = context.facade

        # Models -------------------------------------------------------
        self._asset_model = AssetModel(self._facade)
        self._filmstrip_model = SpacerProxyModel(window)
        self._filmstrip_model.setSourceModel(self._asset_model)

        # Controllers --------------------------------------------------
        self._dialog = DialogController(window, context, window.ui.status_bar)
        self._media = MediaController(window)
        self._playlist = PlaylistController(window)
        self._view_controller = ViewController(
            window.ui.view_stack,
            window.ui.gallery_page,
            window.ui.detail_page,
            window,
        )
        self._player_view_controller = PlayerViewController(
            window.ui.player_stack,
            window.ui.image_viewer,
            window.ui.video_area,
            window.ui.player_placeholder,
            window.ui.live_badge,
            window,
        )
        self._header_controller = HeaderController(
            window.ui.location_label,
            window.ui.timestamp_label,
        )
        self._navigation = NavigationController(
            context,
            self._facade,
            self._asset_model,
            window.ui.sidebar,
            window.ui.album_label,
            window.ui.status_bar,
            self._dialog,
            self._view_controller,
        )
        self._detail_ui = DetailUIController(
            self._asset_model,
            window.ui.filmstrip_view,
            self._player_view_controller,
            window.ui.player_bar,
            self._view_controller,
            self._header_controller,
            window.ui.favorite_button,
            window.ui.status_bar,
            window,
        )
        self._preview_controller = PreviewController(window.ui.preview_window, window)
        self._state_manager = PlaybackStateManager(
            self._media,
            self._playlist,
            self._asset_model,
            self._detail_ui,
            self._dialog,
            window,
        )
        self._playback = PlaybackController(
            self._asset_model,
            self._media,
            self._playlist,
            window.ui.player_bar,
            window.ui.grid_view,
            self._view_controller,
            self._detail_ui,
            self._state_manager,
            self._preview_controller,
            self._facade,
        )
        self._status_bar = StatusBarController(
            window.ui.status_bar,
            window.ui.progress_bar,
            window.ui.rescan_action,
        )

        self._configure_views()
        self._restore_playback_preferences()
        self._playlist.bind_model(self._asset_model)
        self._connect_signals()

    # -----------------------------------------------------------------
    # View configuration
    def _configure_views(self) -> None:
        """Attach models and delegates to the widgets constructed by the UI."""

        self._window.ui.grid_view.setModel(self._asset_model)
        self._window.ui.grid_view.setItemDelegate(
            AssetGridDelegate(self._window.ui.grid_view)
        )

        self._window.ui.filmstrip_view.setModel(self._filmstrip_model)
        self._window.ui.filmstrip_view.setItemDelegate(
            AssetGridDelegate(self._window.ui.filmstrip_view, filmstrip_mode=True)
        )

        self._window.ui.video_area.hide_controls(animate=False)
        self._media.set_video_output(self._window.ui.video_area.video_item)

        self._window.ui.player_bar.setEnabled(False)

    def _restore_playback_preferences(self) -> None:
        """Restore persisted volume and mute state."""

        stored_volume = self._context.settings.get("ui.volume", 75)
        try:
            initial_volume = int(round(float(stored_volume)))
        except (TypeError, ValueError):
            initial_volume = 75
        initial_volume = max(0, min(100, initial_volume))

        stored_muted = self._context.settings.get("ui.is_muted", False)
        if isinstance(stored_muted, str):
            initial_muted = stored_muted.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            initial_muted = bool(stored_muted)

        self._media.set_volume(initial_volume)
        self._media.set_muted(initial_muted)
        self._window.ui.player_bar.set_volume(self._media.volume())
        self._window.ui.player_bar.set_muted(self._media.is_muted())

    # -----------------------------------------------------------------
    # Signal wiring
    def _connect_signals(self) -> None:
        """Connect application, model, and view signals."""

        # Menu and toolbar actions
        self._window.ui.open_album_action.triggered.connect(
            self._handle_open_album_dialog
        )
        self._window.ui.rescan_action.triggered.connect(self._handle_rescan_request)
        # ``QAction.triggered`` emits a boolean ``checked`` flag that our facade method
        # does not accept; use a ``lambda`` wrapper to discard that GUI-specific detail
        # and call the pure application logic with its expected signature.
        self._window.ui.rebuild_links_action.triggered.connect(
            lambda: self._facade.pair_live_current()
        )
        self._window.ui.bind_library_action.triggered.connect(
            self._dialog.bind_library_dialog
        )

        # Global error reporting
        self._facade.errorRaised.connect(self._dialog.show_error)
        self._context.library.errorRaised.connect(self._dialog.show_error)

        # Sidebar navigation
        self._window.ui.sidebar.albumSelected.connect(self.open_album_from_path)
        self._window.ui.sidebar.allPhotosSelected.connect(
            self._navigation.open_all_photos
        )
        self._window.ui.sidebar.staticNodeSelected.connect(
            self._navigation.open_static_node
        )
        self._window.ui.sidebar.bindLibraryRequested.connect(
            self._dialog.bind_library_dialog
        )

        # Facade events
        self._facade.albumOpened.connect(self._handle_album_opened)
        self._facade.scanProgress.connect(self._status_bar.handle_scan_progress)
        self._facade.scanFinished.connect(self._status_bar.handle_scan_finished)
        self._facade.loadStarted.connect(self._status_bar.handle_load_started)
        self._facade.loadProgress.connect(self._status_bar.handle_load_progress)
        self._facade.loadFinished.connect(self._status_bar.handle_load_finished)

        # Model housekeeping
        for signal in (
            self._asset_model.modelReset,
            self._asset_model.rowsInserted,
            self._asset_model.rowsRemoved,
        ):
            signal.connect(self._navigation.update_status)

        self._filmstrip_model.modelReset.connect(
            self._window.ui.filmstrip_view.refresh_spacers
        )

        self._window.ui.grid_view.visibleRowsChanged.connect(
            self._asset_model.prioritize_rows
        )
        self._window.ui.filmstrip_view.visibleRowsChanged.connect(
            self._prioritize_filmstrip_rows
        )

        # View interactions
        for view in (self._window.ui.grid_view, self._window.ui.filmstrip_view):
            view.itemClicked.connect(self._playback.activate_index)
            self._preview_controller.bind_view(view)

        self._playlist.currentChanged.connect(
            self._playback.handle_playlist_current_changed
        )
        self._playlist.sourceChanged.connect(
            self._playback.handle_playlist_source_changed
        )

        # Player bar to playback
        self._window.ui.player_bar.playPauseRequested.connect(
            self._playback.toggle_playback
        )
        self._window.ui.player_bar.volumeChanged.connect(self._media.set_volume)
        self._window.ui.player_bar.muteToggled.connect(self._media.set_muted)
        for signal, slot in (
            (self._window.ui.player_bar.seekRequested, self._media.seek),
            (self._window.ui.player_bar.scrubStarted, self._playback.on_scrub_started),
            (self._window.ui.player_bar.scrubFinished, self._playback.on_scrub_finished),
        ):
            signal.connect(slot)

        # Media engine feedback
        for signal, slot in (
            (
                self._media.positionChanged,
                self._playback.handle_media_position_changed,
            ),
            (self._media.durationChanged, self._window.ui.player_bar.set_duration),
            (
                self._media.playbackStateChanged,
                self._window.ui.player_bar.set_playback_state,
            ),
            (self._media.volumeChanged, self._on_volume_changed),
            (self._media.mutedChanged, self._on_mute_changed),
            (
                self._media.mediaStatusChanged,
                self._playback.handle_media_status_changed,
            ),
            (self._media.errorOccurred, self._dialog.show_error),
        ):
            signal.connect(slot)

        self._window.ui.back_button.clicked.connect(
            self._view_controller.show_gallery_view
        )

    # -----------------------------------------------------------------
    # Slots
    def _handle_open_album_dialog(self) -> None:
        """Display the album picker and open the selected path."""

        path = self._dialog.open_album_dialog()
        if path:
            self.open_album_from_path(path)

    def _handle_rescan_request(self) -> None:
        """Kick off an asynchronous rescan of the current album."""

        if self._facade.current_album is None:
            self._status_bar.show_message("Open an album before rescanning.", 3000)
            return
        self._status_bar.begin_scan()
        self._facade.rescan_current_async()

    def _handle_album_opened(self, root: Path) -> None:
        """React to the facade opening a new or refreshed album."""

        is_detail_view_before_handle = (
            self._window.ui.view_stack.currentWidget() is self._window.ui.detail_page
        )
        was_refresh = self._navigation.consume_last_open_refresh()
        self._navigation.handle_album_opened(root)

        if was_refresh and is_detail_view_before_handle:
            self._view_controller.show_detail_view()
            return

        if (
            self._playlist.current_row() == -1
            and not is_detail_view_before_handle
            and not was_refresh
        ):
            self._view_controller.show_gallery_view()

    def _on_volume_changed(self, volume: int) -> None:
        """Persist volume changes and mirror them to the player bar."""

        clamped = max(0, min(100, int(volume)))
        self._window.ui.player_bar.set_volume(clamped)
        if self._context.settings.get("ui.volume") != clamped:
            self._context.settings.set("ui.volume", clamped)

    def _on_mute_changed(self, muted: bool) -> None:
        """Persist mute toggles and mirror them to the player bar."""

        is_muted = bool(muted)
        self._window.ui.player_bar.set_muted(is_muted)
        if self._context.settings.get("ui.is_muted") != is_muted:
            self._context.settings.set("ui.is_muted", is_muted)

    def _prioritize_filmstrip_rows(self, first: int, last: int) -> None:
        """Prioritise asset loading to match filmstrip visibility."""

        if self._filmstrip_model.rowCount() == 0:
            return

        source_row_count = self._asset_model.rowCount()
        if source_row_count == 0:
            return

        first_source = max(first - 1, 0)
        last_source = min(last - 1, source_row_count - 1)
        if first_source > last_source:
            return
        self._asset_model.prioritize_rows(first_source, last_source)

    # -----------------------------------------------------------------
    # Public helpers used by the window
    def open_album_from_path(self, path: Path) -> None:
        """Forward album navigation requests to the navigation controller."""

        self._navigation.open_album(path)

    def paths_from_indexes(self, indexes: Iterable[QModelIndex]) -> list[Path]:
        """Translate model indexes into absolute filesystem paths."""

        paths: list[Path] = []
        for index in indexes:
            rel = index.data(Roles.REL)
            if rel and self._facade.current_album:
                paths.append((self._facade.current_album.root / rel).resolve())
        return paths

