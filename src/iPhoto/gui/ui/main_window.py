"""Qt widgets composing the main application window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QListView,
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


class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._facade: AppFacade = context.facade
        self._asset_model = AssetModel(self._facade)
        self._album_label = QLabel("Open a folder to browse your photos.")
        self._list_view = QListView()
        self._status = QStatusBar()
        self.setWindowTitle("iPhoto")
        self.resize(1200, 720)
        self._build_ui()
        self._connect_signals()

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
        layout.setContentsMargins(12, 12, 12, 12)
        self._album_label.setObjectName("albumLabel")
        layout.addWidget(self._album_label)

        self._list_view.setModel(self._asset_model)
        self._list_view.setSelectionMode(QListView.ExtendedSelection)
        self._list_view.setViewMode(QListView.IconMode)
        self._list_view.setIconSize(QSize(192, 192))
        self._list_view.setGridSize(QSize(216, 248))
        self._list_view.setSpacing(12)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setResizeMode(QListView.Adjust)
        self._list_view.setMovement(QListView.Static)
        self._list_view.setWrapping(True)
        self._list_view.setWordWrap(True)
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
