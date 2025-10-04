"""Qt widgets composing the main application window."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PySide6.QtCore import QEvent, QItemSelectionModel, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...appctx import AppContext
from ...config import VIDEO_COMPLETE_HOLD_BACKSTEP_MS
from ...errors import LibraryError
from ..facade import AppFacade
from .models.asset_model import AssetModel, Roles
from .widgets.album_sidebar import AlbumSidebar
from .widgets.asset_delegate import AssetGridDelegate
from .widgets.asset_grid import AssetGrid
from .widgets.image_viewer import ImageViewer
from .widgets.player_bar import PlayerBar
from .widgets.preview_window import PreviewWindow
from .media import MediaController, PlaylistController, require_multimedia
from ...utils.deps import load_pillow

_PILLOW = load_pillow()
if _PILLOW is not None:
    _Image = _PILLOW.Image
    _ImageOps = _PILLOW.ImageOps
    _ImageQt = _PILLOW.ImageQt
else:  # pragma: no cover - executed when Pillow is unavailable
    _Image = None  # type: ignore[assignment]
    _ImageOps = None  # type: ignore[assignment]
    _ImageQt = None  # type: ignore[assignment]

if importlib.util.find_spec("PySide6.QtMultimediaWidgets") is not None:
    from PySide6.QtMultimediaWidgets import QVideoWidget
else:  # pragma: no cover - requires optional Qt module
    class QVideoWidget(QWidget):  # type: ignore[misc]
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - fallback
            raise RuntimeError(
                "PySide6.QtMultimediaWidgets is unavailable. Install PySide6 with "
                "QtMultimedia support to enable video playback."
            )


class PlayerSurface(QWidget):
    """Keep the floating player bar anchored over the active viewer widget."""

    def __init__(
        self,
        content: QWidget,
        overlay: QWidget,
        parent: QWidget | None = None,
        *,
        margin: int = 48,
    ) -> None:
        super().__init__(parent)
        self._margin = margin
        self._controls_visible = False
        self._content = content
        self._overlay = overlay
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh_controls)
        self._stacked: QStackedWidget | None = (
            content if isinstance(content, QStackedWidget) else None
        )
        self._host_widget: QWidget | None = None
        self._window_host: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        content.setParent(self)
        layout.addWidget(content)

        self._configure_overlay_window()
        self.destroyed.connect(self._overlay.close)

        if self._stacked is not None:
            self._stacked.currentChanged.connect(self._on_stack_changed)

        self._bind_overlay_host()
        self._ensure_window_filter()

    # ------------------------------------------------------------------
    # Overlay visibility management
    # ------------------------------------------------------------------
    def show_controls(self) -> None:
        """Display the floating overlay controls and keep them on top."""

        self._controls_visible = True
        self._bind_overlay_host()
        self._ensure_window_filter()
        self._sync_overlay_parent()
        self._overlay.show()
        self.refresh_controls()
        self.schedule_refresh()

    def hide_controls(self) -> None:
        """Hide the floating overlay controls."""

        self._controls_visible = False
        self._overlay.hide()
        self._refresh_timer.stop()

    def refresh_controls(self) -> None:
        """Force the overlay to realign with the viewer when visible."""

        if not self._controls_visible:
            return
        self._reposition_overlay()
        self._overlay.update()

    def schedule_refresh(self, delay_ms: int = 0) -> None:
        """Queue a deferred refresh to run after layout/paint settles."""

        if not self._controls_visible:
            return
        self._refresh_timer.stop()
        self._refresh_timer.start(max(0, delay_ms))

    # ------------------------------------------------------------------
    # QWidget API
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().resizeEvent(event)
        self.refresh_controls()

    def showEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().showEvent(event)
        self._ensure_window_filter()
        self.refresh_controls()

    def hideEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().hideEvent(event)
        self._overlay.hide()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _reposition_overlay(self) -> None:
        if not self._controls_visible:
            return
        host = self._host_widget or self
        rect = host.rect()
        available_width = max(0, rect.width() - (2 * self._margin))
        if available_width == 0 or rect.height() <= 0:
            return
        hint = self._overlay.sizeHint()
        overlay_width = min(hint.width(), available_width)
        overlay_height = hint.height()
        host_origin = host.mapToGlobal(rect.topLeft())
        x = host_origin.x() + (rect.width() - overlay_width) // 2
        y = host_origin.y() + max(0, rect.height() - overlay_height - self._margin)
        self._overlay.setGeometry(x, y, overlay_width, overlay_height)
        self._overlay.raise_()

    def eventFilter(self, obj, event):  # pragma: no cover - GUI behaviour
        if obj is self._host_widget and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
            QEvent.Type.Hide,
        }:
            if event.type() == QEvent.Type.Hide:
                self._overlay.hide()
            else:
                self.schedule_refresh()
        if obj is self._window_host and event.type() in {
            QEvent.Type.Move,
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.WindowStateChange,
        }:
            self.schedule_refresh()
        if obj is self._window_host and event.type() == QEvent.Type.Hide:
            self._overlay.hide()
        return super().eventFilter(obj, event)

    def _on_stack_changed(self, _index: int) -> None:
        self._bind_overlay_host()
        self.schedule_refresh()

    def _bind_overlay_host(self) -> None:
        target: QWidget | None = None
        if self._stacked is not None:
            target = self._stacked.currentWidget()
        if target is None:
            target = self._content
        if target is None:
            target = self
        if target is self._host_widget:
            return
        if self._host_widget is not None and self._host_widget is not self:
            self._host_widget.removeEventFilter(self)
        self._host_widget = target
        if self._host_widget is not None and self._host_widget is not self:
            self._host_widget.installEventFilter(self)
        self.schedule_refresh()

    def _ensure_window_filter(self) -> None:
        window = self.window()
        if window is self._window_host:
            return
        if self._window_host is not None:
            self._window_host.removeEventFilter(self)
        self._window_host = window
        if self._window_host is not None:
            self._window_host.installEventFilter(self)

    def _configure_overlay_window(self) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self._overlay.setWindowFlags(flags)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._overlay.hide()

    def _sync_overlay_parent(self) -> None:
        window = self.window()
        if window is self._overlay.parent():
            return
        self._overlay.setParent(window)
        self._configure_overlay_window()


class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        require_multimedia()
        self._context = context
        self._facade: AppFacade = context.facade
        self._asset_model = AssetModel(self._facade)
        self._album_label = QLabel("Open a folder to browse your photos.")
        self._sidebar = AlbumSidebar(context.library, self)
        self._all_photos_active = False
        self._grid_view = AssetGrid()
        self._list_view = AssetGrid()
        self._status = QStatusBar()
        self._video_widget = QVideoWidget()
        self._image_viewer = ImageViewer()
        self._player_placeholder = QLabel("Select a photo or video to preview.")
        self._player_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._player_placeholder.setStyleSheet(
            "background-color: black; color: white; font-size: 16px;"
        )
        self._player_placeholder.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._player_stack = QStackedWidget()
        self._view_stack = QStackedWidget()
        self._gallery_page: QWidget | None = None
        self._detail_page: QWidget | None = None
        self._player_bar = PlayerBar()
        self._player_bar.setEnabled(False)
        self._media = MediaController(self)
        self._media.set_video_output(self._video_widget)
        self._playlist = PlaylistController(self)
        self._playlist.bind_model(self._asset_model)
        self._preview_window = PreviewWindow(self)
        self._back_button = QToolButton()
        self._resume_playback_after_scrub = False
        self.setWindowTitle("iPhoto")
        self.resize(1200, 720)
        self._build_ui()
        self._connect_signals()
        self._player_bar.set_volume(self._media.volume())
        self._player_bar.set_muted(self._media.is_muted())

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        open_action = QAction("Open Album Folder…", self)
        open_action.triggered.connect(self._show_open_dialog)
        rescan_action = QAction("Rescan", self)
        rescan_action.triggered.connect(lambda: self._facade.rescan_current())
        pair_action = QAction("Rebuild Live Links", self)
        pair_action.triggered.connect(lambda: self._facade.pair_live_current())
        bind_library_action = QAction("Set Basic Library…", self)
        bind_library_action.triggered.connect(self._show_bind_library_dialog)

        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(open_action)
        file_menu.addSeparator()
        file_menu.addAction(bind_library_action)
        file_menu.addSeparator()
        file_menu.addAction(rescan_action)
        file_menu.addAction(pair_action)

        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.addAction(open_action)
        toolbar.addAction(rescan_action)
        toolbar.addAction(pair_action)
        self.addToolBar(toolbar)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        self._album_label.setObjectName("albumLabel")
        right_layout.addWidget(self._album_label)

        gallery_page = QWidget()
        self._gallery_page = gallery_page
        gallery_layout = QVBoxLayout(gallery_page)
        gallery_layout.setContentsMargins(0, 0, 0, 0)
        gallery_layout.setSpacing(0)
        self._grid_view.setModel(self._asset_model)
        self._grid_view.setItemDelegate(AssetGridDelegate(self._grid_view))
        self._grid_view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._grid_view.setViewMode(QListView.ViewMode.IconMode)
        self._grid_view.setIconSize(QSize(192, 192))
        self._grid_view.setGridSize(QSize(194, 194))
        self._grid_view.setSpacing(6)
        self._grid_view.setUniformItemSizes(True)
        self._grid_view.setResizeMode(QListView.ResizeMode.Adjust)
        self._grid_view.setMovement(QListView.Movement.Static)
        self._grid_view.setFlow(QListView.Flow.LeftToRight)
        self._grid_view.setWrapping(True)
        self._grid_view.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._grid_view.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._grid_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._grid_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._grid_view.setWordWrap(False)
        gallery_layout.addWidget(self._grid_view)

        detail_page = QWidget()
        self._detail_page = detail_page
        detail_layout = QVBoxLayout(detail_page)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        self._back_button.setText("← Back")
        self._back_button.setToolTip("Return to grid view")
        self._back_button.setAutoRaise(True)
        self._back_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        header_layout.addWidget(self._back_button)
        header_layout.addStretch(1)
        detail_layout.addWidget(header)

        player_container = QWidget()
        player_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        player_layout = QVBoxLayout(player_container)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.setSpacing(6)
        self._video_widget.setMinimumHeight(320)
        self._video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._video_widget.setStyleSheet("background-color: black;")
        self._player_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._player_stack.addWidget(self._player_placeholder)
        self._player_stack.addWidget(self._image_viewer)
        self._player_stack.addWidget(self._video_widget)
        self._player_stack.setCurrentWidget(self._player_placeholder)
        self._player_surface = PlayerSurface(self._player_stack, self._player_bar)
        self._player_surface.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._player_surface.hide_controls()
        self._player_overlay_confirmed = False
        player_layout.addWidget(self._player_surface)
        detail_layout.addWidget(player_container)

        self._list_view.setModel(self._asset_model)
        self._list_view.setItemDelegate(AssetGridDelegate(self._list_view))
        self._list_view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._list_view.setViewMode(QListView.ViewMode.IconMode)
        self._list_view.setIconSize(QSize(192, 192))
        self._list_view.setGridSize(QSize(194, 194))
        self._list_view.setSpacing(6)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self._list_view.setMovement(QListView.Movement.Static)
        self._list_view.setFlow(QListView.Flow.LeftToRight)
        self._list_view.setWrapping(False)
        self._list_view.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._list_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._list_view.setWordWrap(False)
        self._list_view.setStyleSheet(
            "QListView::item { margin: 0px; padding: 0px; }"
        )
        strip_height = self._list_view.iconSize().height() + 24
        self._list_view.setMinimumHeight(strip_height)
        self._list_view.setMaximumHeight(strip_height + 16)
        self._list_view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        detail_layout.addWidget(self._list_view, stretch=0)

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

        self.setCentralWidget(splitter)
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")
        if self._context.library.root() is None:
            QTimer.singleShot(0, self._prompt_for_basic_library)

    def _connect_signals(self) -> None:
        self._facade.errorRaised.connect(self._show_error)
        self._context.library.errorRaised.connect(self._show_error)
        self._sidebar.albumSelected.connect(self.open_album_from_path)
        self._sidebar.allPhotosSelected.connect(self._open_all_photos)
        self._sidebar.bindLibraryRequested.connect(self._show_bind_library_dialog)
        self._facade.albumOpened.connect(self._on_album_opened)
        self._asset_model.modelReset.connect(self._update_status)
        self._asset_model.rowsInserted.connect(self._update_status)
        self._asset_model.rowsRemoved.connect(self._update_status)
        self._grid_view.itemClicked.connect(self._on_grid_item_clicked)
        self._grid_view.requestPreview.connect(self._on_grid_request_preview)
        self._grid_view.previewReleased.connect(self._close_preview_after_release)
        self._grid_view.previewCancelled.connect(self._cancel_preview)
        self._list_view.itemClicked.connect(self._on_filmstrip_item_clicked)
        self._list_view.requestPreview.connect(self._on_filmstrip_request_preview)
        self._list_view.previewReleased.connect(self._close_preview_after_release)
        self._list_view.previewCancelled.connect(self._cancel_preview)
        self._playlist.currentChanged.connect(self._on_playlist_current_changed)
        self._playlist.sourceChanged.connect(self._on_playlist_source_changed)
        self._player_bar.playPauseRequested.connect(self._toggle_playback)
        self._player_bar.previousRequested.connect(self._play_previous)
        self._player_bar.nextRequested.connect(self._play_next)
        self._player_bar.volumeChanged.connect(self._media.set_volume)
        self._player_bar.muteToggled.connect(self._media.set_muted)
        self._player_bar.seekRequested.connect(self._media.seek)
        self._player_bar.scrubStarted.connect(self._on_scrub_started)
        self._player_bar.scrubFinished.connect(self._on_scrub_finished)
        self._media.positionChanged.connect(self._on_media_position_changed)
        self._media.durationChanged.connect(self._player_bar.set_duration)
        self._media.playbackStateChanged.connect(self._player_bar.set_playback_state)
        self._media.playbackStateChanged.connect(
            lambda _state: self._player_surface.schedule_refresh()
        )
        self._media.volumeChanged.connect(self._player_bar.set_volume)
        self._media.mutedChanged.connect(self._player_bar.set_muted)
        self._media.mediaStatusChanged.connect(self._on_media_status_changed)
        self._media.errorOccurred.connect(self._show_error)
        self._back_button.clicked.connect(self._show_gallery_view)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _show_open_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select album")
        if path:
            self.open_album_from_path(Path(path))

    def _show_bind_library_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Basic Library")
        if not path:
            return
        root = Path(path)
        try:
            self._context.library.bind_path(root)
        except LibraryError as exc:
            self._show_error(str(exc))
            return
        bound_root = self._context.library.root()
        if bound_root is not None:
            self._context.settings.set("basic_library_path", str(bound_root))
            self._status.showMessage(f"Basic Library bound to {bound_root}")

    def open_album_from_path(self, path: Path) -> None:
        """Open *path* as the active album."""

        self._all_photos_active = False
        album = self._facade.open_album(path)
        if album is not None:
            self._context.remember_album(album.root)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "iPhoto", message)

    def _on_album_opened(self, root: Path) -> None:
        library_root = self._context.library.root()
        if self._all_photos_active and library_root == root:
            title = AlbumSidebar.ALL_PHOTOS_TITLE
            self._sidebar.select_all_photos()
        else:
            title = (
                self._facade.current_album.manifest.get("title")
                if self._facade.current_album
                else root.name
            )
            self._sidebar.select_path(root)
            self._all_photos_active = False
        self._album_label.setText(f"{title} — {root}")
        self._update_status()
        self._show_gallery_view()

    def _open_all_photos(self) -> None:
        """Open the Basic Library root as an aggregated "All Photos" view."""

        root = self._context.library.root()
        if root is None:
            self._show_bind_library_dialog()
            return
        self._all_photos_active = True
        album = self._facade.open_album(root)
        if album is None:
            self._all_photos_active = False
            return
        album.manifest = {**album.manifest, "title": AlbumSidebar.ALL_PHOTOS_TITLE}

    def _update_status(self) -> None:
        count = self._asset_model.rowCount()
        if count == 0:
            message = "No assets indexed"
        elif count == 1:
            message = "1 asset indexed"
        else:
            message = f"{count} assets indexed"
        self._status.showMessage(message)

    def _prompt_for_basic_library(self) -> None:
        if self._context.library.root() is not None:
            return
        QMessageBox.information(
            self,
            "Bind Basic Library",
            "Select a folder to use as your Basic Library.",
        )
        self._show_bind_library_dialog()

    def _on_grid_item_clicked(self, index) -> None:
        self._activate_index(index)

    def _on_filmstrip_item_clicked(self, index) -> None:
        self._activate_index(index)

    def _activate_index(self, index) -> None:
        if not index or not index.isValid():
            return
        abs_path = index.data(Roles.ABS)
        if not abs_path:
            return
        row = index.row()
        if bool(index.data(Roles.IS_VIDEO)):
            self._show_detail_view()
            self._playlist.set_current(row)
            return
        self._display_image(Path(abs_path), row=row)

    def _on_grid_request_preview(self, index) -> None:
        self._show_preview_for_index(self._grid_view, index)

    def _on_filmstrip_request_preview(self, index) -> None:
        self._show_preview_for_index(self._list_view, index)

    def _show_preview_for_index(self, view: AssetGrid, index) -> None:
        if not index or not index.isValid():
            return
        if not index.data(Roles.IS_VIDEO):
            return
        abs_path = index.data(Roles.ABS)
        if not abs_path:
            return
        rect = view.visualRect(index)
        global_rect = QRect(
            view.viewport().mapToGlobal(rect.topLeft()),
            rect.size(),
        )
        self._preview_window.show_preview(Path(abs_path), global_rect)

    def _close_preview_after_release(self) -> None:
        self._preview_window.close_preview()

    def _cancel_preview(self) -> None:
        self._preview_window.close_preview(False)

    def _display_image(self, source: Path, row: int | None = None) -> None:
        pixmap = self._load_image_pixmap(source)
        if pixmap is None:
            self._status.showMessage(f"Unable to display {source.name}")
            QMessageBox.warning(
                self,
                "iPhoto",
                f"Could not load {source}",
            )
            return
        self._preview_window.close_preview(False)
        self._media.stop()
        self._playlist.clear()
        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._image_viewer.set_pixmap(pixmap)
        self._show_image_surface()
        self._show_detail_view()
        if row is not None:
            self._select_filmstrip_row(row)
        self._status.showMessage(f"Viewing {source.name}")

    def _on_playlist_current_changed(self, row: int) -> None:
        selection_model = self._list_view.selectionModel()
        if selection_model is None:
            return
        if row < 0:
            self._player_bar.reset()
            self._player_bar.setEnabled(False)
            self._media.stop()
            self._show_player_placeholder()
            selection_model.clearSelection()
            return
        selection_model.clearSelection()
        index = self._asset_model.index(row, 0)
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        self._list_view.scrollTo(index)
        self._player_bar.setEnabled(True)
        self._show_detail_view()

    def _on_playlist_source_changed(self, source: Path) -> None:
        self._preview_window.close_preview(False)
        self._media.stop()
        self._media.load(source)
        self._player_bar.set_position(0)
        self._player_bar.set_duration(0)
        self._show_video_surface()
        self._show_detail_view()
        self._media.play()
        self._status.showMessage(f"Playing {source.name}")

    def _on_media_status_changed(self, status: object) -> None:
        """Raise the overlay whenever playback surfaces update."""

        # ``QMediaPlayer.MediaStatus`` values expose a ``name`` attribute. We
        # keep the guard tolerant of unexpected objects to avoid crashes when
        # optional QtMultimedia backends differ.
        name = getattr(status, "name", None)
        if name == "EndOfMedia":
            self._freeze_video_final_frame()
            return
        if name in {"LoadedMedia", "BufferingMedia", "BufferedMedia", "StalledMedia"}:
            self._player_surface.refresh_controls()
            self._player_surface.schedule_refresh(120)

    def _on_media_position_changed(self, position_ms: int) -> None:
        """Update progress UI and ensure the overlay stays visible."""

        self._player_bar.set_position(position_ms)
        if position_ms > 0 and not self._player_overlay_confirmed:
            # The first non-zero frame confirms the video widget is actively
            # rendering. Re-raise the floating controls so they cannot be
            # obscured by late-arriving native surfaces.
            self._player_surface.refresh_controls()
            self._player_surface.schedule_refresh(60)
            self._player_overlay_confirmed = True

    def _toggle_playback(self) -> None:
        """Pause/resume playback, restarting finished videos from the start."""

        state = self._media.playback_state()
        playing = getattr(state, "name", None) == "PlayingState"
        if not playing:
            duration = self._player_bar.duration()
            if duration > 0 and self._player_bar.position() >= duration:
                self._media.seek(0)
                self._player_bar.set_position(0)
        self._media.toggle()

    def _on_scrub_started(self) -> None:
        """Pause playback while the user drags the progress slider."""

        state = self._media.playback_state()
        self._resume_playback_after_scrub = getattr(state, "name", "") == "PlayingState"
        if self._resume_playback_after_scrub:
            self._media.pause()

    def _on_scrub_finished(self) -> None:
        """Resume playback if it was active before scrubbing."""

        if self._resume_playback_after_scrub:
            self._media.play()
        self._resume_playback_after_scrub = False
    def _play_previous(self) -> None:
        self._playlist.previous()

    def _play_next(self) -> None:
        self._playlist.next()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    def current_selection(self) -> list[Path]:
        """Return the currently selected assets as absolute paths."""

        indexes = self._list_view.selectionModel().selectedIndexes()
        paths: list[Path] = []
        for index in indexes:
            rel = self._asset_model.data(index, Roles.REL)
            if rel and self._facade.current_album:
                paths.append((self._facade.current_album.root / rel).resolve())
        return paths

    # ------------------------------------------------------------------
    # Player presentation helpers
    # ------------------------------------------------------------------
    def _load_image_pixmap(self, source: Path) -> QPixmap | None:
        reader = QImageReader(str(source))
        reader.setAutoTransform(True)
        image = reader.read()
        if not image.isNull():
            pixmap = QPixmap.fromImage(image)
            if not pixmap.isNull():
                return pixmap
        # Qt's image plugins omit HEIF/HEIC on some platforms. Fall back to Pillow when
        # available so that the inline viewer can display those assets.
        fallback = self._load_image_with_pillow(source)
        if fallback is not None:
            return fallback
        return None

    def _load_image_with_pillow(self, source: Path) -> QPixmap | None:
        if _Image is None or _ImageOps is None or _ImageQt is None:
            return None
        try:
            with _Image.open(source) as img:  # type: ignore[attr-defined]
                img = _ImageOps.exif_transpose(img)  # type: ignore[attr-defined]
                qt_image = _ImageQt(img.convert("RGBA"))  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - Pillow loader failure propagates as warning
            return None
        qimage = QImage(qt_image)
        pixmap = QPixmap.fromImage(qimage)
        if pixmap.isNull():
            return None
        return pixmap

    def _show_player_placeholder(self) -> None:
        """Ensure the placeholder is visible when nothing is selected."""

        self._player_surface.hide_controls()
        self._player_overlay_confirmed = False
        self._resume_playback_after_scrub = False
        if self._player_stack.currentWidget() is not self._player_placeholder:
            self._player_stack.setCurrentWidget(self._player_placeholder)
        self._image_viewer.clear()

    def _show_video_surface(self) -> None:
        """Reveal the video widget inside the stacked player area."""

        if self._player_stack.currentWidget() is not self._video_widget:
            self._player_stack.setCurrentWidget(self._video_widget)
        self._player_bar.setEnabled(True)
        self._player_overlay_confirmed = False
        self._resume_playback_after_scrub = False
        self._player_surface.show_controls()
        # Ensure the overlay is raised once the video widget has attached and
        # started rendering frames.
        self._player_surface.schedule_refresh()
        self._player_surface.schedule_refresh(150)

    def _freeze_video_final_frame(self) -> None:
        """Hold the last decoded frame on screen when playback completes."""

        if self._player_stack.currentWidget() is not self._video_widget:
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
        self._player_surface.refresh_controls()
        self._player_surface.schedule_refresh(60)

    def _show_image_surface(self) -> None:
        self._player_surface.hide_controls()
        self._player_overlay_confirmed = False
        self._resume_playback_after_scrub = False
        if self._player_stack.currentWidget() is not self._image_viewer:
            self._player_stack.setCurrentWidget(self._image_viewer)

    def _show_detail_view(self) -> None:
        if self._detail_page is not None and self._view_stack.currentWidget() is not self._detail_page:
            self._view_stack.setCurrentWidget(self._detail_page)

    def _show_gallery_view(self) -> None:
        self._preview_window.close_preview(False)
        self._media.stop()
        self._playlist.clear()
        self._player_bar.reset()
        self._player_bar.setEnabled(False)
        self._show_player_placeholder()
        self._image_viewer.clear()
        self._list_view.clearSelection()
        self._grid_view.clearSelection()
        if self._gallery_page is not None:
            self._view_stack.setCurrentWidget(self._gallery_page)
        self._status.showMessage("Browse your library")

    def _select_filmstrip_row(self, row: int) -> None:
        selection_model = self._list_view.selectionModel()
        if selection_model is None or row < 0:
            return
        index = self._asset_model.index(row, 0)
        selection_model.setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._list_view.scrollTo(index)
