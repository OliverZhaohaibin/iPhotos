"""Coordinator that wires the main window to application logic."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, TYPE_CHECKING

from PySide6.QtCore import QModelIndex, QObject, QThreadPool, Qt

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
from ..widgets import AssetGridDelegate, InfoPanel, NotificationToast
from .context_menu_controller import ContextMenuController
from .detail_ui_controller import DetailUIController
from .dialog_controller import DialogController
from .drag_drop_controller import DragDropController
from .header_controller import HeaderController
from .navigation_controller import NavigationController
from .map_view_controller import LocationMapController
from .playback_controller import PlaybackController
from .playback_state_manager import PlaybackStateManager
from .player_view_controller import PlayerViewController
from .preference_controller import PreferenceController
from .preview_controller import PreviewController
from .selection_controller import SelectionController
from .share_controller import ShareController
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
        self._grid_delegate: AssetGridDelegate | None = None

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
            map_page=window.ui.map_page,
            parent=window,
        )
        self._player_view_controller = PlayerViewController(
            window.ui.player_stack,
            window.ui.image_viewer,
            window.ui.video_area,
            window.ui.player_placeholder,
            window.ui.live_badge,
            window,
        )
        self._info_panel = InfoPanel(window)
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
        self._map_controller = LocationMapController(
            context.library,
            self._playlist,
            self._view_controller,
            window.ui.map_view,
            window,
        )
        self._detail_ui = DetailUIController(
            self._asset_model,
            window.ui.filmstrip_view,
            self._player_view_controller,
            window.ui.player_bar,
            self._view_controller,
            self._header_controller,
            window.ui.favorite_button,
            window.ui.info_button,
            self._info_panel,
            window.ui.zoom_widget,
            window.ui.zoom_slider,
            window.ui.zoom_in_button,
            window.ui.zoom_out_button,
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
        # The notification toast confirms clipboard interactions without cluttering
        # the status bar.  It is instantiated once and reused to minimise
        # allocations and keep the animations silky-smooth when triggered rapidly.
        self._notification_toast = NotificationToast(window)

        window.ui.selection_button.setEnabled(False)

        self._configure_views()

        self._selection_controller = SelectionController(
            window.ui.selection_button,
            window.ui.grid_view,
            self._grid_delegate,
            self._preview_controller,
            self._playback,
            parent=window,
        )
        self._preference_controller = PreferenceController(
            settings=context.settings,
            media=self._media,
            player_bar=window.ui.player_bar,
            filmstrip_view=window.ui.filmstrip_view,
            filmstrip_action=window.ui.toggle_filmstrip_action,
            wheel_action_group=window.ui.wheel_action_group,
            wheel_action_zoom=window.ui.wheel_action_zoom,
            wheel_action_navigate=window.ui.wheel_action_navigate,
            image_viewer=window.ui.image_viewer,
            parent=window,
        )
        self._share_controller = ShareController(
            settings=context.settings,
            playlist=self._playlist,
            asset_model=self._asset_model,
            status_bar=self._status_bar,
            notification_toast=self._notification_toast,
            share_button=window.ui.share_button,
            share_action_group=window.ui.share_action_group,
            copy_file_action=window.ui.share_action_copy_file,
            copy_path_action=window.ui.share_action_copy_path,
            reveal_action=window.ui.share_action_reveal_file,
            parent=window,
        )
        self._share_controller.restore_preference()
        self._context_menu_controller = ContextMenuController(
            grid_view=window.ui.grid_view,
            asset_model=self._asset_model,
            facade=self._facade,
            navigation=self._navigation,
            status_bar=self._status_bar,
            notification_toast=self._notification_toast,
            selection_controller=self._selection_controller,
            parent=window,
        )
        self._drag_drop_controller = DragDropController(
            grid_view=window.ui.grid_view,
            sidebar=window.ui.sidebar,
            context=context,
            facade=self._facade,
            status_bar=self._status_bar,
            dialog=self._dialog,
            navigation=self._navigation,
            parent=window,
        )
        self._playlist.bind_model(self._asset_model)
        self._connect_signals()

    def shutdown(self) -> None:
        """Stop worker threads and background jobs before the app exits."""

        # The map view spawns two dedicated threads (tile streaming and
        # clustering).  Closing the widget triggers ``PhotoMapView.closeEvent``
        # which, in turn, shuts down the ``TileManager`` and marker worker.
        self._map_controller.shutdown()

        # Thumbnail rendering uses both the global and a private thread pool.
        # Clearing and waiting here ensures no ``QRunnable`` outlives the
        # QApplication, avoiding Qt warnings about leaked threads.
        self._asset_model.thumbnail_loader().shutdown()

        # ``QThreadPool.globalInstance()`` may still be flushing one-off tasks
        # (library scans, metadata reads, etc.).  Waiting guarantees the main
        # process only exits after every background job has finished.
        QThreadPool.globalInstance().waitForDone()

    # -----------------------------------------------------------------
    # View configuration
    def _configure_views(self) -> None:
        """Attach models and delegates to the widgets constructed by the UI."""

        self._window.ui.grid_view.setModel(self._asset_model)
        self._grid_delegate = AssetGridDelegate(self._window.ui.grid_view)
        self._window.ui.grid_view.setItemDelegate(self._grid_delegate)
        self._window.ui.grid_view.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )

        self._window.ui.filmstrip_view.setModel(self._filmstrip_model)
        self._window.ui.filmstrip_view.setItemDelegate(
            AssetGridDelegate(self._window.ui.filmstrip_view, filmstrip_mode=True)
        )

        self._window.ui.video_area.hide_controls(animate=False)
        self._media.set_video_output(self._window.ui.video_area.video_item)

        self._window.ui.player_bar.setEnabled(False)

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
        # ``treeUpdated`` fires whenever the filesystem watcher rebuilds the
        # sidebar hierarchy.  Forward the notification so the navigation
        # controller can decide whether the resulting selection changes should
        # trigger a fresh navigation cycle.
        self._context.library.treeUpdated.connect(
            self._navigation.handle_tree_updated
        )

        # Sidebar navigation
        self._window.ui.sidebar.albumSelected.connect(self.open_album_from_path)
        self._window.ui.sidebar.allPhotosSelected.connect(
            self._handle_all_photos_selected
        )
        self._window.ui.sidebar.staticNodeSelected.connect(
            self._handle_static_node_selected
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
        self._facade.indexUpdated.connect(self._map_controller.handle_index_update)

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
        self._preview_controller.bind_view(self._window.ui.grid_view)
        self._window.ui.filmstrip_view.itemClicked.connect(
            self._playback.activate_index
        )
        self._preview_controller.bind_view(self._window.ui.filmstrip_view)

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
        ):
            signal.connect(slot)

        # Media engine feedback
        for signal, slot in (
            (
                self._media.positionChanged,
                self._detail_ui.set_player_position,
            ),
            (self._media.durationChanged, self._detail_ui.set_player_duration),
            (
                self._media.playbackStateChanged,
                self._detail_ui.set_playback_state,
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

    def _handle_all_photos_selected(self) -> None:
        """Reset to the default gallery view when All Photos is selected."""

        if (
            self._navigation.should_suppress_tree_refresh()
            and self._navigation.is_all_photos_view()
        ):
            return
        self._map_controller.hide_map_view()
        self._selection_controller.set_selection_mode(False)
        self._navigation.open_all_photos()

    def _handle_static_node_selected(self, title: str) -> None:
        """Dispatch sidebar selections, treating Location as a special case."""

        if self._navigation.should_suppress_tree_refresh():
            current_static = self._navigation.static_selection()
            if current_static and current_static.casefold() == title.casefold():
                return
        self._selection_controller.set_selection_mode(False)
        if title.casefold() == "location":
            self._navigation.open_location_view()
            if self._context.library.root() is None:
                self._map_controller.hide_map_view()
                return
            self._map_controller.refresh_assets()
            self._map_controller.show_map_view()
            return
        self._map_controller.hide_map_view()
        self._navigation.open_static_node(title)

    def _handle_import_finished(
        self,
        _root: Path | None,
        success: bool,
        _message: str,
    ) -> None:
        """Drop tree-refresh guards and refresh caches after imports."""

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
        """Release tree-refresh guards and refresh ancillary views after moves."""

        self._navigation.clear_tree_refresh_suppression()
        if success:
            # The map controller caches geotagged entries across the entire
            # library.  Refreshing here ensures moved assets immediately appear
            # under their new album hierarchy without waiting for a manual
            # rescan.
            self._map_controller.refresh_assets()

    def _handle_album_opened(self, root: Path) -> None:
        """React to the facade opening a new or refreshed album."""

        is_detail_view_before_handle = (
            self._window.ui.view_stack.currentWidget() is self._window.ui.detail_page
        )
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

        if self._navigation.should_suppress_tree_refresh():
            current_album = self._facade.current_album
            if current_album is not None:
                try:
                    if current_album.root.resolve() == Path(path).resolve():
                        return
                except OSError:
                    # If resolving either path fails we fall back to the
                    # default behaviour so the navigation request is still
                    # honoured for user-initiated selections.
                    pass
        self._map_controller.hide_map_view()
        self._navigation.open_album(path)

    def paths_from_indexes(self, indexes: Iterable[QModelIndex]) -> list[Path]:
        """Translate model indexes into absolute filesystem paths."""

        paths: list[Path] = []
        for index in indexes:
            rel = index.data(Roles.REL)
            if rel and self._facade.current_album:
                paths.append((self._facade.current_album.root / rel).resolve())
        return paths

    # -----------------------------------------------------------------
    # Drag-and-drop helpers
    def _validate_grid_drop(self, paths: List[Path]) -> bool:
        """Return ``True`` when the active grid can accept at least one path."""

        images, videos, _ = self._classify_media_paths(paths)

        # Reject early when absolutely nothing in the payload is supported.  This keeps
        # the UX responsive while still allowing the handler to cherry-pick valid files
        # from a mixed drop that also contained unsupported entries.
        if not images and not videos:
            return False

        selection = (self._navigation.static_selection() or "").casefold()

        # Live Photos rely on separate pairing logic and therefore cannot be imported
        # via direct drag-and-drop into the dedicated view.
        if selection == "live photos":
            return False

        # The Videos view should respond positively when at least one dropped file is a
        # video.  The handler will filter the payload down to the allowed subset.
        if selection == "videos":
            return bool(videos)

        # Every other gallery (All Photos, Favorites, user albums, etc.) can import any
        # mixture of images or videos as long as at least one supported asset is present.
        return bool(images or videos)

    def _handle_grid_drop(self, paths: List[Path]) -> None:
        """Import the dropped files into the active gallery view."""

        images, videos, _ = self._classify_media_paths(paths)
        selection = (self._navigation.static_selection() or "").casefold()
        target: Optional[Path]
        mark_featured = False

        if selection == "videos":
            allowed = videos
        else:
            allowed = images + videos
            mark_featured = selection == "favorites"

        # Inform the user when the drop only contained unsupported files or nothing that
        # matches the requirements of the active view.
        if not allowed:
            self._status_bar.show_message("No supported media files were dropped.", 5000)
            return

        if selection:
            target = self._context.library.root()
            if target is None:
                self._dialog.bind_library_dialog()
                return
        else:
            album = self._facade.current_album
            if album is None:
                self._status_bar.show_message(
                    "Open an album before importing files.",
                    5000,
                )
                return
            target = album.root

        self._facade.import_files(
            allowed,
            destination=target,
            mark_featured=mark_featured,
        )

    def _handle_sidebar_drop(self, target: Path, payload: object) -> None:
        """Import files that were dropped onto *target* in the sidebar."""

        paths = self._coerce_path_list(payload)
        if not paths:
            return
        images, videos, unsupported = self._classify_media_paths(paths)
        if unsupported:
            self._status_bar.show_message(
                "Only photo and video files can be imported.",
                5000,
            )
            return
        allowed = images + videos
        if not allowed:
            self._status_bar.show_message("No supported media files were dropped.", 5000)
            return
        self._facade.import_files(allowed, destination=target)

    def _classify_media_paths(self, paths: Iterable[Path]) -> tuple[List[Path], List[Path], List[Path]]:
        """Split *paths* into images, videos, and unsupported candidates."""

        normalized = self._normalize_drop_paths(paths)
        images: List[Path] = []
        videos: List[Path] = []
        unsupported: List[Path] = []
        for path in normalized:
            suffix = path.suffix.lower()
            if not path.exists() or not path.is_file():
                unsupported.append(path)
                continue
            if suffix in IMAGE_EXTENSIONS:
                images.append(path)
            elif suffix in VIDEO_EXTENSIONS:
                videos.append(path)
            else:
                unsupported.append(path)
        return images, videos, unsupported

    def _normalize_drop_paths(self, paths: Iterable[Path]) -> List[Path]:
        """Return canonical, deduplicated paths suitable for importing."""

        normalized: List[Path] = []
        seen: set[Path] = set()
        for path in paths:
            try:
                candidate = Path(path).expanduser()
            except TypeError:
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved in seen:
                continue
            seen.add(resolved)
            normalized.append(resolved)
        return normalized

    def _coerce_path_list(self, payload: object) -> List[Path]:
        """Best-effort conversion of signal payloads into ``Path`` objects."""

        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, tuple):
            items = list(payload)
        else:
            items = [payload]
        paths: List[Path] = []
        for item in items:
            try:
                paths.append(Path(item))
            except TypeError:
                continue
        return paths

