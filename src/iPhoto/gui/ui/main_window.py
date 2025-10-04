"""Qt widgets composing the main application window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QRect, QSize
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ...appctx import AppContext
from ..facade import AppFacade
from .models.asset_model import AssetModel, Roles
from .widgets.asset_delegate import AssetGridDelegate
from .widgets.asset_grid import AssetGrid
from .widgets.player_bar import PlayerBar
from .widgets.preview_window import PreviewWindow
from .media import MediaController, PlaylistController


class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._facade: AppFacade = context.facade
        self._asset_model = AssetModel(self._facade)
        self._album_label = QLabel("Open a folder to browse your photos.")
        self._list_view = AssetGrid()
        self._status = QStatusBar()
        self._video_widget = QVideoWidget()
        self._player_bar = PlayerBar()
        self._player_bar.setEnabled(False)
        self._media = MediaController(self)
        self._media.set_video_output(self._video_widget)
        self._playlist = PlaylistController(self)
        self._playlist.bind_model(self._asset_model)
        self._preview_window = PreviewWindow(self)
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
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        open_action = toolbar.addAction("Open Album…")
        open_action.triggered.connect(lambda _: self._show_open_dialog())
        rescan_action = toolbar.addAction("Rescan")
        rescan_action.triggered.connect(lambda _: self._facade.rescan_current())
        pair_action = toolbar.addAction("Rebuild Live Links")
        pair_action.triggered.connect(lambda _: self._facade.pair_live_current())
        self.addToolBar(toolbar)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        self._album_label.setObjectName("albumLabel")
        layout.addWidget(self._album_label)

        player_container = QWidget()
        player_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        player_layout = QVBoxLayout(player_container)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.setSpacing(6)
        self._video_widget.setMinimumHeight(320)
        self._video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._video_widget.setStyleSheet("background-color: black;")
        player_layout.addWidget(self._video_widget)
        player_layout.addWidget(self._player_bar)
        layout.addWidget(player_container)

        self._list_view.setModel(self._asset_model)
        self._list_view.setItemDelegate(AssetGridDelegate(self._list_view))
        self._list_view.setSelectionMode(AssetGrid.ExtendedSelection)
        self._list_view.setViewMode(AssetGrid.IconMode)
        self._list_view.setIconSize(QSize(192, 192))
        self._list_view.setGridSize(QSize(194, 194))
        self._list_view.setSpacing(2)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setResizeMode(AssetGrid.Adjust)
        self._list_view.setMovement(AssetGrid.Static)
        self._list_view.setWrapping(True)
        self._list_view.setWordWrap(False)
        self._list_view.setStyleSheet(
            "QListView::item { margin: 0px; padding: 0px; }"
        )
        self._list_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._list_view, stretch=1)

        self.setCentralWidget(container)
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

    def _connect_signals(self) -> None:
        self._facade.errorRaised.connect(self._show_error)
        self._facade.albumOpened.connect(self._on_album_opened)
        self._asset_model.modelReset.connect(self._update_status)
        self._asset_model.rowsInserted.connect(self._update_status)
        self._asset_model.rowsRemoved.connect(self._update_status)
        self._list_view.itemClicked.connect(self._on_item_clicked)
        self._list_view.requestPreview.connect(self._on_request_preview)
        self._list_view.previewReleased.connect(self._close_preview_after_release)
        self._list_view.previewCancelled.connect(self._cancel_preview)
        self._playlist.currentChanged.connect(self._on_playlist_current_changed)
        self._playlist.sourceChanged.connect(self._on_playlist_source_changed)
        self._player_bar.playPauseRequested.connect(self._media.toggle)
        self._player_bar.previousRequested.connect(self._play_previous)
        self._player_bar.nextRequested.connect(self._play_next)
        self._player_bar.volumeChanged.connect(self._media.set_volume)
        self._player_bar.muteToggled.connect(self._media.set_muted)
        self._player_bar.seekRequested.connect(self._media.seek)
        self._media.positionChanged.connect(self._player_bar.set_position)
        self._media.durationChanged.connect(self._player_bar.set_duration)
        self._media.playbackStateChanged.connect(self._player_bar.set_playback_state)
        self._media.volumeChanged.connect(self._player_bar.set_volume)
        self._media.mutedChanged.connect(self._player_bar.set_muted)
        self._media.errorOccurred.connect(self._show_error)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _show_open_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select album")
        if path:
            self.open_album_from_path(Path(path))

    def open_album_from_path(self, path: Path) -> None:
        """Open *path* as the active album."""

        album = self._facade.open_album(path)
        if album is not None:
            self._context.remember_album(album.root)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "iPhoto", message)

    def _on_album_opened(self, root: Path) -> None:
        title = self._facade.current_album.manifest.get("title") if self._facade.current_album else root.name
        self._album_label.setText(f"{title} — {root}")
        self._update_status()

    def _update_status(self) -> None:
        count = self._asset_model.rowCount()
        if count == 0:
            message = "No assets indexed"
        elif count == 1:
            message = "1 asset indexed"
        else:
            message = f"{count} assets indexed"
        self._status.showMessage(message)

    def _on_item_clicked(self, index) -> None:
        if not index.isValid():
            return
        if not index.data(Roles.IS_VIDEO):
            return
        self._playlist.set_current(index.row())

    def _on_request_preview(self, index) -> None:
        if not index or not index.isValid():
            return
        if not index.data(Roles.IS_VIDEO):
            return
        abs_path = index.data(Roles.ABS)
        if not abs_path:
            return
        rect = self._list_view.visualRect(index)
        global_rect = QRect(
            self._list_view.viewport().mapToGlobal(rect.topLeft()),
            rect.size(),
        )
        self._preview_window.show_preview(Path(abs_path), global_rect)

    def _close_preview_after_release(self) -> None:
        self._preview_window.close_preview()

    def _cancel_preview(self) -> None:
        self._preview_window.close_preview(False)

    def _on_playlist_current_changed(self, row: int) -> None:
        selection_model = self._list_view.selectionModel()
        if selection_model is None:
            return
        selection_model.clearSelection()
        if row < 0:
            self._player_bar.reset()
            self._player_bar.setEnabled(False)
            return
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

    def _on_playlist_source_changed(self, source: Path) -> None:
        self._preview_window.close_preview(False)
        self._media.stop()
        self._media.load(source)
        self._player_bar.set_position(0)
        self._player_bar.set_duration(0)
        self._media.play()
        self._status.showMessage(f"Playing {source.name}")

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
