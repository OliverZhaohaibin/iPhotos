"""Coordinator that wires the main window to application logic."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, TYPE_CHECKING

from PySide6.QtCore import QModelIndex, QObject, QThreadPool

from ...facade import AppFacade
from ..models.asset_model import Roles
from .data_manager import DataManager
from .dialog_controller import DialogController
from .interaction_manager import InteractionManager
from .navigation_controller import NavigationController
from .status_bar_controller import StatusBarController
from .view_controller_manager import ViewControllerManager

if TYPE_CHECKING:  # pragma: no cover - import used for typing only
    from ....appctx import AppContext
    from ..main_window import MainWindow


class MainController(QObject):
    """High-level coordinator for the main window."""

    def __init__(self, window: "MainWindow", context: AppContext) -> None:
        super().__init__(window)
        self._window = window
        self._context = context
        self._facade: AppFacade = context.facade

        # Data layer ----------------------------------------------------
        self._data = DataManager(window, self._facade)
        self._data.configure_views(window.ui)

        # Controllers ---------------------------------------------------
        self._dialog = DialogController(window, context, window.ui.status_bar)
        self._status_bar = StatusBarController(
            window.ui.status_bar,
            window.ui.progress_bar,
            window.ui.rescan_action,
            context,
        )
        self._view_manager = ViewControllerManager(window, context, self._data)
        self._navigation = NavigationController(
            context,
            self._facade,
            self._data.asset_model(),
            window.ui.sidebar,
            window.ui.album_label,
            window.ui.status_bar,
            self._dialog,
            self._view_manager.view_controller(),
        )
        self._interaction = InteractionManager(
            window=window,
            context=context,
            facade=self._facade,
            data_manager=self._data,
            view_manager=self._view_manager,
            navigation=self._navigation,
            dialog=self._dialog,
            status_bar=self._status_bar,
            window_manager=window.window_manager,
            main_controller=self,
        )

        # Cached shortcuts to frequently used controllers ----------------
        self._playlist = self._data.playlist()
        self._asset_model = self._data.asset_model()
        self._filmstrip_model = self._data.filmstrip_model()
        self._view_controller = self._view_manager.view_controller()
        self._detail_ui = self._view_manager.detail_ui()
        self._map_controller = self._view_manager.map_controller()
        self._playback = self._interaction.playback()
        self._state_manager = self._interaction.state_manager()
        self._selection_controller = self._interaction.selection()
        self._edit_controller = self._view_manager.edit_controller()

        self._playlist.bind_model(self._asset_model)
        self._connect_signals()

    # -----------------------------------------------------------------
    # Lifecycle management
    def shutdown(self) -> None:
        """Stop worker threads and background jobs before the app exits."""

        self._interaction.shutdown()
        self._map_controller.shutdown()
        self._asset_model.thumbnail_loader().shutdown()
        QThreadPool.globalInstance().waitForDone()

    # -----------------------------------------------------------------
    # Signal wiring
    def _connect_signals(self) -> None:
        """Connect application, model, and view signals."""

        ui = self._window.ui

        # Menu and toolbar actions
        ui.open_album_action.triggered.connect(self._handle_open_album_dialog)
        ui.rescan_action.triggered.connect(self._handle_rescan_request)
        ui.rebuild_links_action.triggered.connect(lambda: self._facade.pair_live_current())
        ui.bind_library_action.triggered.connect(self._dialog.bind_library_dialog)

        # Global error reporting
        self._facade.errorRaised.connect(self._dialog.show_error)
        self._context.library.errorRaised.connect(self._dialog.show_error)
        self._context.library.treeUpdated.connect(self._navigation.handle_tree_updated)

        # Sidebar navigation
        ui.sidebar.albumSelected.connect(self.open_album_from_path)
        ui.sidebar.allPhotosSelected.connect(self._handle_all_photos_selected)
        ui.sidebar.staticNodeSelected.connect(self._handle_static_node_selected)
        ui.sidebar.bindLibraryRequested.connect(self._dialog.bind_library_dialog)

        # Facade events
        self._facade.albumOpened.connect(self._handle_album_opened)
        updates = self._facade.library_updates
        updates.scanProgress.connect(self._status_bar.handle_scan_progress)
        updates.scanFinished.connect(self._status_bar.handle_scan_finished)
        updates.indexUpdated.connect(self._map_controller.handle_index_update)
        self._facade.loadStarted.connect(self._status_bar.handle_load_started)
        self._facade.loadProgress.connect(self._status_bar.handle_load_progress)
        self._facade.loadFinished.connect(self._status_bar.handle_load_finished)

        import_service = self._facade.import_service
        import_service.importStarted.connect(self._status_bar.handle_import_started)
        import_service.importProgress.connect(self._status_bar.handle_import_progress)
        import_service.importFinished.connect(self._status_bar.handle_import_finished)
        import_service.importFinished.connect(self._handle_import_finished)

        move_service = self._facade.move_service
        move_service.moveStarted.connect(self._status_bar.handle_move_started)
        move_service.moveProgress.connect(self._status_bar.handle_move_progress)
        move_service.moveFinished.connect(self._status_bar.handle_move_finished)
        move_service.moveFinished.connect(self._handle_move_finished)

        # Model housekeeping
        for signal in (
            self._asset_model.modelReset,
            self._asset_model.rowsInserted,
            self._asset_model.rowsRemoved,
        ):
            signal.connect(self._navigation.update_status)

        self._filmstrip_model.modelReset.connect(ui.filmstrip_view.refresh_spacers)
        ui.grid_view.visibleRowsChanged.connect(self._asset_model.prioritize_rows)
        ui.filmstrip_view.visibleRowsChanged.connect(self._prioritize_filmstrip_rows)

        # View interactions
        preview = self._interaction.preview()
        preview.bind_view(ui.grid_view)
        ui.filmstrip_view.itemClicked.connect(self._playback.activate_index)
        preview.bind_view(ui.filmstrip_view)

        self._playlist.currentChanged.connect(
            self._playback.handle_playlist_current_changed
        )
        self._playlist.sourceChanged.connect(
            self._playback.handle_playlist_source_changed
        )

        # Player bar to playback
        ui.player_bar.playPauseRequested.connect(self._playback.toggle_playback)
        ui.player_bar.volumeChanged.connect(self._data.media().set_volume)
        ui.player_bar.muteToggled.connect(self._data.media().set_muted)
        ui.player_bar.seekRequested.connect(self._data.media().seek)

        # Media engine feedback
        for signal, slot in (
            (self._data.media().positionChanged, self._detail_ui.set_player_position),
            (self._data.media().durationChanged, self._detail_ui.set_player_duration),
            (self._data.media().playbackStateChanged, self._detail_ui.set_playback_state),
            (self._data.media().volumeChanged, self._on_volume_changed),
            (self._data.media().mutedChanged, self._on_mute_changed),
            (
                self._data.media().mediaStatusChanged,
                self._playback.handle_media_status_changed,
            ),
            (self._data.media().errorOccurred, self._dialog.show_error),
        ):
            signal.connect(slot)

        ui.back_button.clicked.connect(self._handle_back_button_clicked)
        ui.edit_button.clicked.connect(self._edit_controller.begin_edit)
        # Clearing navigation suppression after edits finish allows sidebar
        # refreshes triggered by the filesystem watcher to proceed normally once
        # the detail pane has settled back into its post-edit state.
        self._edit_controller.editingFinished.connect(
            self._navigation.clear_tree_refresh_suppression
        )

    # -----------------------------------------------------------------
    # Slots
    def _handle_open_album_dialog(self) -> None:
        path = self._dialog.open_album_dialog()
        if path:
            self.open_album_from_path(path)

    def _handle_back_button_clicked(self) -> None:
        self._edit_controller.leave_edit_mode()
        self._playback.reset_for_gallery_navigation()
        self._view_controller.show_gallery_view()

    def _handle_rescan_request(self) -> None:
        if self._facade.current_album is None:
            self._status_bar.show_message("Open an album before rescanning.", 3000)
            return
        self._status_bar.begin_scan()
        self._facade.rescan_current_async()

    def _handle_all_photos_selected(self) -> None:
        if (
            self._navigation.should_suppress_tree_refresh()
            and self._navigation.is_all_photos_view()
        ):
            return
        self._map_controller.hide_map_view()
        self._selection_controller.set_selection_mode(False)
        self._navigation.open_all_photos()

    def _handle_static_node_selected(self, title: str) -> None:
        if self._navigation.should_suppress_tree_refresh():
            current_static = self._navigation.static_selection()
            if current_static and current_static.casefold() == title.casefold():
                return

        self._selection_controller.set_selection_mode(False)
        lowered = title.casefold()
        if lowered == "location":
            self._navigation.open_location_view()
            if self._context.library.root() is None:
                self._map_controller.hide_map_view()
                return
            self._map_controller.refresh_assets()
            self._map_controller.show_map_view()
            return
        if lowered == "recently deleted":
            self._map_controller.hide_map_view()
            self._navigation.open_recently_deleted()
            return

        self._map_controller.hide_map_view()
        self._navigation.open_static_node(title)

    def _handle_import_finished(
        self,
        _root: Path | None,
        success: bool,
        _message: str,
    ) -> None:
        self._navigation.clear_tree_refresh_suppression()
        if success:
            self._map_controller.refresh_assets()

    def _handle_move_finished(
        self,
        _source: Path,
        _destination: Path,
        success: bool,
        _message: str,
    ) -> None:
        self._navigation.clear_tree_refresh_suppression()
        if success:
            self._map_controller.refresh_assets()

    def _handle_album_opened(self, root: Path) -> None:
        is_detail_view_before_handle = self._view_controller.is_detail_view_active()
        was_refresh = self._navigation.consume_last_open_refresh()
        self._navigation.handle_album_opened(root)
        self._window.ui.selection_button.setEnabled(True)
        self._selection_controller.set_selection_mode(False)

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
        clamped = max(0, min(100, int(volume)))
        self._window.ui.player_bar.set_volume(clamped)
        if self._context.settings.get("ui.volume") != clamped:
            self._context.settings.set("ui.volume", clamped)

    def _on_mute_changed(self, muted: bool) -> None:
        is_muted = bool(muted)
        self._window.ui.player_bar.set_muted(is_muted)
        if self._context.settings.get("ui.is_muted") != is_muted:
            self._context.settings.set("ui.is_muted", is_muted)

    def _prioritize_filmstrip_rows(self, first: int, last: int) -> None:
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
    # Public helpers used by the window and shortcut controller
    def toggle_playback(self) -> None:
        self._playback.toggle_playback()

    def replay_live_photo(self) -> None:
        self._playback.replay_live_photo()

    def request_next_item(self) -> None:
        self._playback.request_next_item()

    def request_previous_item(self) -> None:
        self._playback.request_previous_item()

    def current_player_state(self):
        return self._state_manager.state

    def is_media_muted(self) -> bool:
        return self._data.media().is_muted()

    def set_media_muted(self, muted: bool) -> None:
        self._data.media().set_muted(muted)

    def media_volume(self) -> int:
        return int(self._data.media().volume())

    def set_media_volume(self, volume: int) -> None:
        clamped = max(0, min(100, int(volume)))
        self._data.media().set_volume(clamped)

    def open_album_from_path(self, path: Path) -> None:
        if self._navigation.should_suppress_tree_refresh():
            current_album = self._facade.current_album
            if current_album is not None:
                try:
                    if current_album.root.resolve() == Path(path).resolve():
                        return
                except OSError:
                    pass
        self._map_controller.hide_map_view()
        self._navigation.open_album(path)

    def paths_from_indexes(self, indexes: Iterable[QModelIndex]) -> list[Path]:
        paths: list[Path] = []
        for index in indexes:
            rel = index.data(Roles.REL)
            if rel and self._facade.current_album:
                paths.append((self._facade.current_album.root / rel).resolve())
        return paths

    def prepare_fullscreen_asset(self) -> bool:
        if self._asset_model.rowCount() == 0:
            if not self._navigation.is_all_photos_view():
                self._navigation.open_all_photos()
            if self._asset_model.rowCount() == 0:
                self._detail_ui.show_detail_view()
                return False

        current_row = self._playlist.current_row()
        if current_row != -1:
            self._detail_ui.show_detail_view()
            return True

        grid_selection = self._window.ui.grid_view.selectionModel()
        if grid_selection is not None:
            candidate = grid_selection.currentIndex()
            if not candidate.isValid():
                selected = grid_selection.selectedIndexes()
                if selected:
                    candidate = selected[0]
            if candidate.isValid():
                self._playback.activate_index(candidate)
                if self._playlist.current_row() != -1:
                    return True

        if self._asset_model.rowCount() > 0:
            first_index = self._asset_model.index(0, 0)
            if first_index.isValid():
                self._playback.activate_index(first_index)
                if self._playlist.current_row() != -1:
                    return True

        self._detail_ui.show_detail_view()
        return False

    def show_placeholder_in_viewer(self) -> None:
        self._detail_ui.show_detail_view()
        self._detail_ui.show_placeholder()

    def suspend_playback_for_transition(self) -> bool:
        if not self._data.media().is_playing():
            return False
        self._data.media().pause()
        return True

    def resume_playback_after_transition(self) -> None:
        if self._data.media().is_paused():
            self._data.media().play()

