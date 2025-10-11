"""Qt widgets composing the main application window."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, Qt, QSize
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ``main_window`` can be imported either via ``iPhoto.gui`` (package execution)
# or ``iPhotos.src.iPhoto.gui`` (legacy test harness).  The absolute import
# keeps script-mode launches working where the relative form lacks package
# context.
try:  # pragma: no cover - exercised in packaging scenarios
    from ...appctx import AppContext
except ImportError:  # pragma: no cover - script execution fallback
    from iPhotos.src.iPhoto.appctx import AppContext
from ..facade import AppFacade
from .controllers.dialog_controller import DialogController
from .controllers.header_controller import HeaderController
from .controllers.navigation_controller import NavigationController
from .controllers.player_view_controller import PlayerViewController
from .controllers.playback_controller import PlaybackController
from .controllers.view_controller import ViewController
from .media import MediaController, PlaylistController, require_multimedia
from .models.asset_model import AssetModel, Roles
from .models.spacer_proxy_model import SpacerProxyModel
from .widgets import (
    AlbumSidebar,
    AssetGridDelegate,
    FilmstripView,
    GalleryGridView,
    ImageViewer,
    VideoArea,
    PreviewWindow,
    LiveBadge,
)
from .icons import load_icon

class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        require_multimedia()
        self._context = context
        self._facade: AppFacade = context.facade
        self._asset_model = AssetModel(self._facade)
        self._filmstrip_model = SpacerProxyModel(self)
        self._filmstrip_model.setSourceModel(self._asset_model)
        self._status = QStatusBar()
        self._sidebar = AlbumSidebar(context.library, self)
        self._album_label = QLabel("Open a folder to browse your photos.")
        self._grid_view = GalleryGridView()
        self._filmstrip_view = FilmstripView()
        self._video_area = VideoArea()
        self._player_bar = self._video_area.player_bar
        self._media = MediaController(self)
        self._playlist = PlaylistController(self)
        self._preview_window = PreviewWindow(self)
        self._image_viewer = ImageViewer()
        self._player_placeholder = QLabel("Select a photo or video to preview.")
        self._player_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._player_placeholder.setStyleSheet(
            "background-color: black; color: white; font-size: 16px;"
        )
        self._player_placeholder.setMinimumHeight(320)
        self._player_stack = QStackedWidget()
        self._view_stack = QStackedWidget()
        self._gallery_page = self._detail_page = None
        self._back_button = QToolButton()
        self._info_button = QToolButton()
        self._share_button = QToolButton()
        self._favorite_button = QToolButton()
        self._live_badge = LiveBadge(self)
        self._live_badge.hide()
        self._badge_host: QWidget | None = None
        self._location_label = QLabel()
        self._timestamp_label = QLabel()

        self._dialog = DialogController(self, context, self._status)
        self._rescan_action: QAction | None = None
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setVisible(False)
        self._progress_bar.setMinimumWidth(160)
        self._progress_bar.setTextVisible(False)
        self._progress_context: Optional[str] = None

        stored_volume = self._context.settings.get("ui.volume", 75)
        try:
            initial_volume = int(round(float(stored_volume)))
        except (TypeError, ValueError):
            initial_volume = 75
        initial_volume = max(0, min(100, initial_volume))
        stored_muted = self._context.settings.get("ui.is_muted", False)
        if isinstance(stored_muted, str):
            initial_muted = stored_muted.strip().lower() in {"1", "true", "yes", "on"}
        else:
            initial_muted = bool(stored_muted)
        self._media.set_volume(initial_volume)
        self._media.set_muted(initial_muted)

        self._build_ui()
        self._view_controller = ViewController(
            self._view_stack,
            self._gallery_page,
            self._detail_page,
            self,
        )
        self._player_view_controller = PlayerViewController(
            self._player_stack,
            self._image_viewer,
            self._video_area,
            self._player_placeholder,
            self._live_badge,
            self,
        )
        self._header_controller = HeaderController(
            self._location_label,
            self._timestamp_label,
        )
        self._navigation = NavigationController(
            context,
            self._facade,
            self._asset_model,
            self._sidebar,
            self._album_label,
            self._status,
            self._dialog,
        )
        self._playback = PlaybackController(
            self._asset_model,
            self._media,
            self._playlist,
            self._player_bar,
            self._grid_view,
            self._filmstrip_view,
            self._preview_window,
            self._player_view_controller,
            self._view_controller,
            self._header_controller,
            self._status,
            self._dialog,
            self._facade,
            self._favorite_button,
        )

        self._connect_signals()
        self._playlist.bind_model(self._asset_model)
        self._filmstrip_model.modelReset.connect(self._filmstrip_view.refresh_spacers)
        self._player_bar.setEnabled(False)
        self._player_bar.set_volume(self._media.volume())
        self._player_bar.set_muted(self._media.is_muted())
        self._status.addPermanentWidget(self._progress_bar)

    # UI setup helpers
    def _build_ui(self) -> None:
        self.setWindowTitle("iPhoto")
        self.resize(1200, 720)
        self.setStatusBar(self._status)

        self._build_actions()
        self._configure_views()
        self.setCentralWidget(self._build_splitter())

    def _build_actions(self) -> None:
        open_action = QAction("Open Album Folder…", self)
        open_action.triggered.connect(self._handle_open_album_dialog)
        self._rescan_action = QAction("Rescan", self)
        self._rescan_action.triggered.connect(self._handle_rescan_request)
        pair_action = QAction("Rebuild Live Links", self)
        pair_action.triggered.connect(lambda: self._facade.pair_live_current())
        bind_library_action = QAction("Set Basic Library…", self)
        bind_library_action.triggered.connect(self._dialog.bind_library_dialog)

        file_menu = self.menuBar().addMenu("&File")
        for action in (
            open_action,
            None,
            bind_library_action,
            None,
            self._rescan_action,
            pair_action,
        ):
            file_menu.addSeparator() if action is None else file_menu.addAction(action)

        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        for action in (open_action, self._rescan_action, pair_action):
            toolbar.addAction(action)
        self.addToolBar(toolbar)

    def _configure_views(self) -> None:
        self._grid_view.setModel(self._asset_model)
        self._grid_view.setItemDelegate(AssetGridDelegate(self._grid_view))
        self._grid_view.visibleRowsChanged.connect(self._asset_model.prioritize_rows)

        self._filmstrip_view.setModel(self._filmstrip_model)
        self._filmstrip_view.setItemDelegate(
            AssetGridDelegate(self._filmstrip_view, filmstrip_mode=True)
        )
        self._filmstrip_view.visibleRowsChanged.connect(self._prioritize_filmstrip_rows)

        self._player_stack.addWidget(self._player_placeholder)
        self._player_stack.addWidget(self._image_viewer)
        self._player_stack.addWidget(self._video_area)
        self._player_stack.setCurrentWidget(self._player_placeholder)
        self._video_area.hide_controls(animate=False)
        self._media.set_video_output(self._video_area.video_item)

    def _build_splitter(self) -> QSplitter:
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        self._album_label.setObjectName("albumLabel")
        right_layout.addWidget(self._album_label)

        gallery_page = QWidget()
        gallery_layout = QVBoxLayout(gallery_page)
        gallery_layout.setContentsMargins(0, 0, 0, 0)
        gallery_layout.setSpacing(0)
        gallery_layout.addWidget(self._grid_view)
        self._gallery_page = gallery_page

        detail_page = QWidget()
        detail_layout = QVBoxLayout(detail_page)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)

        icon_size = QSize(24, 24)
        self._back_button.setIcon(load_icon("chevron.left.svg"))
        self._back_button.setIconSize(icon_size)
        self._back_button.setToolTip("Return to grid view")
        self._back_button.setAutoRaise(True)
        self._back_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        header_layout.addWidget(self._back_button)

        info_container = QWidget()
        info_layout = QVBoxLayout(info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        info_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        base_font = self.font()
        location_font = QFont(base_font)
        if location_font.pointSize() > 0:
            location_font.setPointSize(location_font.pointSize() + 2)
        else:
            location_font.setPointSize(14)
        location_font.setBold(True)

        timestamp_font = QFont(base_font)
        if timestamp_font.pointSize() > 0:
            timestamp_font.setPointSize(max(timestamp_font.pointSize() + 1, 1))
        else:
            timestamp_font.setPointSize(12)
        timestamp_font.setBold(False)

        self._location_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._location_label.setFont(location_font)
        self._location_label.setVisible(False)

        self._timestamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._timestamp_label.setFont(timestamp_font)
        self._timestamp_label.setVisible(False)

        info_layout.addWidget(self._location_label)
        info_layout.addWidget(self._timestamp_label)
        header_layout.addWidget(info_container, 1)

        actions_container = QWidget()
        actions_layout = QHBoxLayout(actions_container)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(4)

        def _configure_action_button(button: QToolButton, icon_name: str, tooltip: str) -> None:
            """Assign a consistent icon-only appearance to header actions."""

            button.setIcon(load_icon(icon_name))
            button.setIconSize(icon_size)
            button.setAutoRaise(True)
            button.setToolTip(tooltip)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        _configure_action_button(self._info_button, "info.circle.svg", "Show photo information")
        _configure_action_button(self._share_button, "square.and.arrow.up.svg", "Share this item")
        _configure_action_button(self._favorite_button, "suit.heart.svg", "Mark as Favorite")
        self._favorite_button.setEnabled(False)

        for button in (self._info_button, self._share_button, self._favorite_button):
            actions_layout.addWidget(button)

        header_layout.addWidget(actions_container)
        detail_layout.addWidget(header)

        player_container = QWidget()
        player_layout = QVBoxLayout(player_container)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.setSpacing(0)
        player_layout.addWidget(self._player_stack)
        detail_layout.addWidget(player_container)
        detail_layout.addWidget(self._filmstrip_view)
        self._detail_page = detail_page

        self._live_badge.setParent(player_container)
        self._badge_host = player_container
        self._position_live_badge()
        player_container.installEventFilter(self)

        self._view_stack.addWidget(gallery_page)
        self._view_stack.addWidget(detail_page)
        self._view_stack.setCurrentWidget(gallery_page)
        right_layout.addWidget(self._view_stack)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        return splitter

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

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_live_badge()

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched is self._badge_host and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
        }:
            self._position_live_badge()
        return super().eventFilter(watched, event)

    def _position_live_badge(self) -> None:
        if self._badge_host is None:
            return
        self._live_badge.move(15, 15)
        self._live_badge.raise_()

    # Signal wiring
    def _connect_signals(self) -> None:
        self._facade.errorRaised.connect(self._dialog.show_error)
        self._context.library.errorRaised.connect(self._dialog.show_error)
        self._sidebar.albumSelected.connect(self.open_album_from_path)
        self._sidebar.allPhotosSelected.connect(self._navigation.open_all_photos)
        self._sidebar.staticNodeSelected.connect(self._navigation.open_static_node)
        self._sidebar.bindLibraryRequested.connect(self._dialog.bind_library_dialog)
        self._facade.albumOpened.connect(self._handle_album_opened)
        self._facade.scanProgress.connect(self._on_scan_progress)
        self._facade.scanFinished.connect(self._on_scan_finished)
        self._facade.loadStarted.connect(self._on_load_started)
        self._facade.loadProgress.connect(self._on_load_progress)
        self._facade.loadFinished.connect(self._on_load_finished)

        for signal in (
            self._asset_model.modelReset,
            self._asset_model.rowsInserted,
            self._asset_model.rowsRemoved,
        ):
            signal.connect(self._navigation.update_status)

        for view in (self._grid_view, self._filmstrip_view):
            view.itemClicked.connect(self._playback.activate_index)
            view.requestPreview.connect(
                partial(self._playback.show_preview_for_index, view)
            )
            view.previewReleased.connect(self._playback.close_preview_after_release)
            view.previewCancelled.connect(self._playback.cancel_preview)

        self._playlist.currentChanged.connect(self._playback.handle_playlist_current_changed)
        self._playlist.sourceChanged.connect(self._playback.handle_playlist_source_changed)

        self._player_bar.playPauseRequested.connect(self._playback.toggle_playback)
        self._player_bar.volumeChanged.connect(self._media.set_volume)
        self._player_bar.muteToggled.connect(self._media.set_muted)
        for signal, slot in (
            (self._player_bar.seekRequested, self._media.seek),
            (self._player_bar.scrubStarted, self._playback.on_scrub_started),
            (self._player_bar.scrubFinished, self._playback.on_scrub_finished),
        ):
            signal.connect(slot)

        for signal, slot in (
            (self._media.positionChanged, self._playback.handle_media_position_changed),
            (self._media.durationChanged, self._player_bar.set_duration),
            (self._media.playbackStateChanged, self._player_bar.set_playback_state),
            (self._media.volumeChanged, self._on_volume_changed),
            (self._media.mutedChanged, self._on_mute_changed),
            (self._media.mediaStatusChanged, self._playback.handle_media_status_changed),
            (self._media.errorOccurred, self._dialog.show_error),
        ):
            signal.connect(slot)
        self._back_button.clicked.connect(self._view_controller.show_gallery_view)

    # Public API used by sidebar/actions
    def open_album_from_path(self, path: Path) -> None:
        self._navigation.open_album(path)

    # Slots
    def _handle_open_album_dialog(self) -> None:
        path = self._dialog.open_album_dialog()
        if path:
            self.open_album_from_path(path)

    def _handle_rescan_request(self) -> None:
        if self._facade.current_album is None:
            self._status.showMessage("Open an album before rescanning.", 3000)
            return
        if self._rescan_action is not None:
            self._rescan_action.setEnabled(False)
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        self._progress_context = "scan"
        self._status.showMessage("Starting scan…")
        self._facade.rescan_current_async()

    def _handle_album_opened(self, root: Path) -> None:
        self._navigation.handle_album_opened(root)
        self._view_controller.show_gallery_view()

    def _on_scan_progress(self, root: Path, current: int, total: int) -> None:
        if self._progress_context not in {"scan", None}:
            return
        if self._progress_context is None:
            self._progress_context = "scan"
            self._progress_bar.setValue(0)
            self._progress_bar.setVisible(True)
        if total < 0:
            self._progress_bar.setRange(0, 0)
            self._status.showMessage("Scanning… (counting files)")
        elif total == 0:
            self._progress_bar.setRange(0, 0)
            self._status.showMessage("Scanning… (no files found)")
        else:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(max(0, min(current, total)))
            self._status.showMessage(f"Scanning… ({current}/{total})")
        self._progress_bar.setVisible(True)

    def _on_scan_finished(self, root: Path | None, success: bool) -> None:
        if self._progress_context == "scan":
            self._progress_bar.setVisible(False)
            self._progress_bar.setRange(0, 0)
            self._progress_context = None
        if self._rescan_action is not None:
            self._rescan_action.setEnabled(True)
        message = "Scan complete." if success else "Scan failed."
        self._status.showMessage(message, 5000)

    def _on_load_started(self, root: Path) -> None:
        self._progress_context = "load"
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        self._status.showMessage("Loading items…")

    def _on_load_progress(self, root: Path, current: int, total: int) -> None:
        if self._progress_context != "load":
            return
        if total <= 0:
            self._progress_bar.setRange(0, 0)
        else:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(max(0, min(current, total)))
        if total > 0:
            self._status.showMessage(f"Loading items… ({current}/{total})")

    def _on_load_finished(self, root: Path, success: bool) -> None:
        if self._progress_context != "load":
            return
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 0)
        self._progress_context = None
        message = "Album loaded." if success else "Failed to load album."
        self._status.showMessage(message, 5000)

    def _on_volume_changed(self, volume: int) -> None:
        clamped = max(0, min(100, int(volume)))
        self._player_bar.set_volume(clamped)
        if self._context.settings.get("ui.volume") != clamped:
            self._context.settings.set("ui.volume", clamped)

    def _on_mute_changed(self, muted: bool) -> None:
        self._player_bar.set_muted(bool(muted))
        if self._context.settings.get("ui.is_muted") != bool(muted):
            self._context.settings.set("ui.is_muted", bool(muted))

    # Convenience
    def current_selection(self) -> list[Path]:
        indexes = self._filmstrip_view.selectionModel().selectedIndexes()
        paths: list[Path] = []
        for index in indexes:
            rel = index.data(Roles.REL)
            if rel and self._facade.current_album:
                paths.append((self._facade.current_album.root / rel).resolve())
        return paths
