"""Qt widgets composing the main application window."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QRect, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...appctx import AppContext
from ..facade import AppFacade
from .models.asset_model import AssetModel, Roles
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
    """Container that keeps the player controls floating over the viewer."""

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

        base_layout = QVBoxLayout(self)
        base_layout.setContentsMargins(0, 0, 0, 0)
        base_layout.setSpacing(0)

        self._content_container = QWidget(self)
        content_layout = QVBoxLayout(self._content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content.setParent(self._content_container)
        content_layout.addWidget(content)
        base_layout.addWidget(self._content_container)

        self._overlay_container = QWidget(self)
        self._overlay_container.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._overlay_container.setAttribute(
            Qt.WidgetAttribute.WA_NoSystemBackground, True
        )
        overlay_layout = QVBoxLayout(self._overlay_container)
        overlay_layout.setContentsMargins(
            self._margin, self._margin, self._margin, self._margin
        )
        overlay_layout.setSpacing(0)
        overlay_layout.addStretch(1)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addStretch(1)
        overlay.setParent(self._overlay_container)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        row.addWidget(overlay)
        row.addStretch(1)
        overlay_layout.addLayout(row)
        self._overlay_container.hide()
        overlay.hide()

    def show_controls(self) -> None:
        """Display the floating overlay controls."""

        self._controls_visible = True
        self._overlay_container.setGeometry(self.rect())
        self._overlay_container.show()
        self._overlay.show()
        self._overlay_container.raise_()
        self._overlay.raise_()
        self._overlay.adjustSize()

    def hide_controls(self) -> None:
        """Hide the floating overlay controls."""

        self._controls_visible = False
        self._overlay.hide()
        self._overlay_container.hide()

    def resizeEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        self._overlay_container.setGeometry(self.rect())
        super().resizeEvent(event)

    def showEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        self._overlay_container.setGeometry(self.rect())
        super().showEvent(event)


class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        require_multimedia()
        self._context = context
        self._facade: AppFacade = context.facade
        self._asset_model = AssetModel(self._facade)
        self._album_label = QLabel("Open a folder to browse your photos.")
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
        layout.addWidget(self._view_stack)

        self.setCentralWidget(container)
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

    def _connect_signals(self) -> None:
        self._facade.errorRaised.connect(self._show_error)
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
        self._back_button.clicked.connect(self._show_gallery_view)

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
        self._show_gallery_view()

    def _update_status(self) -> None:
        count = self._asset_model.rowCount()
        if count == 0:
            message = "No assets indexed"
        elif count == 1:
            message = "1 asset indexed"
        else:
            message = f"{count} assets indexed"
        self._status.showMessage(message)

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
        if self._player_stack.currentWidget() is not self._player_placeholder:
            self._player_stack.setCurrentWidget(self._player_placeholder)
        self._image_viewer.clear()

    def _show_video_surface(self) -> None:
        """Reveal the video widget inside the stacked player area."""

        if self._player_stack.currentWidget() is not self._video_widget:
            self._player_stack.setCurrentWidget(self._video_widget)
        self._player_bar.setEnabled(True)
        self._player_surface.show_controls()

    def _show_image_surface(self) -> None:
        self._player_surface.hide_controls()
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
