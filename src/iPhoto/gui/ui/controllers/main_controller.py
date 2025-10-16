"""Coordinator that wires the main window to application logic."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtCore import QModelIndex

# ``main_controller`` shares the same import caveat as ``main_window``.  The
# fallback ensures running the module as a script still locates ``AppContext``.
try:  # pragma: no cover - exercised in packaging scenarios
    from ....appctx import AppContext
except ImportError:  # pragma: no cover - script execution fallback
    from iPhotos.src.iPhoto.appctx import AppContext

from ...facade import AppFacade
from ....media_classifier import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from ..media import MediaController, PlaylistController
from ..models.asset_model import AssetModel, Roles
from ..models.spacer_proxy_model import SpacerProxyModel
from ..widgets import AlbumSidebar, AssetGridDelegate
from .detail_ui_controller import DetailUIController
from .dialog_controller import DialogController
from .header_controller import HeaderController
from .navigation_controller import NavigationController
from .map_view_controller import LocationMapController
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
        self._window.ui.grid_view.configure_external_drop(
            handler=self._handle_grid_drop,
            validator=self._validate_grid_drop,
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
            self._handle_all_photos_selected
        )
        self._window.ui.sidebar.staticNodeSelected.connect(
            self._handle_static_node_selected
        )
        self._window.ui.sidebar.bindLibraryRequested.connect(
            self._dialog.bind_library_dialog
        )
        self._window.ui.sidebar.filesDropped.connect(self._handle_sidebar_drop)

        # Facade events
        self._facade.albumOpened.connect(self._handle_album_opened)
        self._facade.scanProgress.connect(self._status_bar.handle_scan_progress)
        self._facade.scanFinished.connect(self._status_bar.handle_scan_finished)
        self._facade.loadStarted.connect(self._status_bar.handle_load_started)
        self._facade.loadProgress.connect(self._status_bar.handle_load_progress)
        self._facade.loadFinished.connect(self._status_bar.handle_load_finished)
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

        self._map_controller.hide_map_view()
        self._navigation.open_all_photos()

    def _handle_static_node_selected(self, title: str) -> None:
        """Dispatch sidebar selections, treating Location as a special case."""

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
        """Return ``True`` if the current view can accept *paths*."""

        images, videos, unsupported = self._classify_media_paths(paths)
        if unsupported:
            return False
        selection = (self._navigation.static_selection() or "").casefold()
        if selection == "live photos":
            return False
        if selection == "videos":
            return bool(videos) and not images
        if selection in {"", AlbumSidebar.ALL_PHOTOS_TITLE.casefold(), "favorites"}:
            return bool(images or videos)
        return False

    def _handle_grid_drop(self, paths: List[Path]) -> None:
        """Import the dropped files into the active gallery view."""

        images, videos, unsupported = self._classify_media_paths(paths)
        if unsupported:
            self._status_bar.show_message(
                "Only photo and video files can be imported.",
                5000,
            )
            return

        selection = (self._navigation.static_selection() or "").casefold()
        target: Optional[Path]
        mark_featured = False

        if selection == "videos":
            if not videos or images:
                self._status_bar.show_message(
                    "The Videos view only accepts video files.",
                    5000,
                )
                return
            allowed = videos
        else:
            allowed = images + videos
            mark_featured = selection == "favorites"

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

        imported = self._facade.import_files(
            allowed,
            destination=target,
            mark_featured=mark_featured,
        )
        if not imported:
            self._status_bar.show_message("No files were imported.", 5000)
            return
        label = "file" if len(imported) == 1 else "files"
        self._status_bar.show_message(
            f"Imported {len(imported)} {label}.",
            4000,
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
        imported = self._facade.import_files(allowed, destination=target)
        if not imported:
            self._status_bar.show_message("No files were imported.", 5000)
            return
        label = "file" if len(imported) == 1 else "files"
        self._status_bar.show_message(
            f"Imported {len(imported)} {label} into {target.name}.",
            4000,
        )

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

