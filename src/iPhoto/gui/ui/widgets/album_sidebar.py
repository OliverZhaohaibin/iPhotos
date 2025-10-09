"""Sidebar widget presenting the Basic Library tree."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QModelIndex, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ....errors import LibraryError
from ....library.manager import LibraryManager
from ....library.tree import AlbumNode
from ..models.album_tree_model import (
    AlbumTreeItem,
    AlbumTreeModel,
    NodeType,
)
from .album_sidebar_view import AlbumTreeView

# ---------------------------------------------------------------------------
# Sidebar styling helpers
# ---------------------------------------------------------------------------

SIDEBAR_BG_COLOR = QColor("#e8e8ea")


class AlbumSidebar(QWidget):
    """Composite widget exposing library navigation and actions."""

    albumSelected = Signal(Path)
    allPhotosSelected = Signal()
    staticNodeSelected = Signal(str)
    bindLibraryRequested = Signal()

    ALL_PHOTOS_TITLE = (
        AlbumTreeModel.STATIC_NODES[0]
        if AlbumTreeModel.STATIC_NODES
        else "All Photos"
    )

    def __init__(self, library: LibraryManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self._model = AlbumTreeModel(library, self)
        self._pending_selection: Path | None = None
        self._current_selection: Path | None = None
        self._current_static_selection: str | None = None

        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, SIDEBAR_BG_COLOR)
        palette.setColor(QPalette.ColorRole.Base, SIDEBAR_BG_COLOR)
        self.setPalette(palette)
        self.setAutoFillBackground(True)

        self._title = QLabel("Basic Library")
        self._title.setObjectName("albumSidebarTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        title_font = QFont(self._title.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 0.5)
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._title.setStyleSheet("color: #1b1b1b;")

        self._tree = AlbumTreeView(self)
        self._tree.setObjectName("albumSidebarTree")
        self._tree.setModel(self._model)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._tree.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._tree.doubleClicked.connect(self._on_double_clicked)
        self._tree.setMinimumWidth(220)
        self._tree.setIconSize(QSize(18, 18))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._title)
        layout.addWidget(self._tree, stretch=1)

        self._model.modelReset.connect(self._on_model_reset)
        self._expand_defaults()
        self._update_title()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _expand_defaults(self) -> None:
        """Expand high-level nodes to match the reference layout."""

        if self._model.rowCount() == 0:
            return
        root_index = self._model.index(0, 0)
        if root_index.isValid():
            self._tree.expand(root_index)
            for row in range(self._model.rowCount(root_index)):
                child = self._model.index(row, 0, root_index)
                if child.isValid():
                    self._tree.expand(child)

    def _on_model_reset(self) -> None:
        self._update_title()
        self._expand_defaults()
        if self._pending_selection is not None:
            self.select_path(self._pending_selection)
            self._pending_selection = None
        elif self._current_selection is not None:
            self.select_path(self._current_selection)
        elif self._current_static_selection:
            self.select_static_node(self._current_static_selection)

    def _update_title(self) -> None:
        root = self._library.root()
        if root is None:
            self._title.setText("Basic Library — not bound")
        else:
            self._title.setText(f"Basic Library — {root}")

    def _on_selection_changed(self, _selected, _deselected) -> None:
        index = self._tree.currentIndex()
        item = self._model.item_from_index(index)
        if item is None:
            return
        node_type = item.node_type
        if node_type == NodeType.ACTION:
            self.bindLibraryRequested.emit()
            return
        if node_type == NodeType.STATIC:
            if self._library.root() is None:
                self.bindLibraryRequested.emit()
                return
            self._current_selection = None
            self._current_static_selection = item.title
            if item.title == self.ALL_PHOTOS_TITLE:
                self.allPhotosSelected.emit()
            else:
                self.staticNodeSelected.emit(item.title)
            return
        self._current_static_selection = None
        album = item.album
        if album is not None:
            self._current_selection = album.path
            self.albumSelected.emit(album.path)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        item = self._model.item_from_index(index)
        if item is None:
            return
        if item.node_type == NodeType.ACTION:
            self.bindLibraryRequested.emit()

    def select_path(self, path: Path) -> None:
        """Select the tree item corresponding to *path* if it exists."""

        index = self._model.index_for_path(path)
        if not index.isValid():
            return
        self._current_static_selection = None
        self._tree.setCurrentIndex(index)
        self._tree.scrollTo(index)

    def select_all_photos(self) -> None:
        """Select the "All Photos" static node if it is available."""

        self.select_static_node(self.ALL_PHOTOS_TITLE)

    def select_static_node(self, title: str) -> None:
        """Select the static node matching *title* when present."""

        index = self._find_static_index(title)
        if not index.isValid():
            return
        self._current_selection = None
        self._current_static_selection = title
        self._tree.setCurrentIndex(index)
        self._tree.scrollTo(index)

    def _show_context_menu(self, point: QPoint) -> None:
        index = self._tree.indexAt(point)
        global_pos = self._tree.viewport().mapToGlobal(point)
        if not index.isValid():
            menu = QMenu(self)
            menu.addAction("Set Basic Library…", self.bindLibraryRequested.emit)
            menu.exec(global_pos)
            return
        item = self._model.item_from_index(index)
        if item is None:
            return
        menu = QMenu(self)
        if item.node_type in {NodeType.HEADER, NodeType.SECTION}:
            menu.addAction("New Album…", self._prompt_new_album)
        if item.node_type == NodeType.ALBUM:
            menu.addAction("New Sub-Album…", lambda: self._prompt_new_album(item))
            menu.addAction("Rename Album…", lambda: self._prompt_rename_album(item))
            menu.addSeparator()
            menu.addAction("Show in File Manager", lambda: self._reveal_path(item.album))
        if item.node_type == NodeType.SUBALBUM:
            menu.addAction("Rename Album…", lambda: self._prompt_rename_album(item))
            menu.addSeparator()
            menu.addAction("Show in File Manager", lambda: self._reveal_path(item.album))
        if item.node_type == NodeType.ACTION:
            menu.addAction("Set Basic Library…", self.bindLibraryRequested.emit)
        if not menu.isEmpty():
            menu.exec(global_pos)

    def _prompt_new_album(self, parent_item: Optional[AlbumTreeItem] = None) -> None:
        base_item = None
        if parent_item is None:
            index = self._tree.currentIndex()
            base_item = self._model.item_from_index(index)
        else:
            base_item = parent_item

        if base_item is None:
            return
        name, ok = QInputDialog.getText(self, "New Album", "Album name:")
        if not ok:
            return
        target_name = name.strip()
        if not target_name:
            QMessageBox.warning(self, "iPhoto", "Album name cannot be empty.")
            return
        try:
            if base_item.node_type == NodeType.ALBUM and base_item.album is not None:
                node = self._library.create_subalbum(base_item.album, target_name)
            else:
                node = self._library.create_album(target_name)
        except LibraryError as exc:  # pragma: no cover - GUI feedback
            QMessageBox.warning(self, "iPhoto", str(exc))
            return
        self._pending_selection = node.path

    def _prompt_rename_album(self, item) -> None:
        if item.album is None:
            return
        current_title = item.album.title
        name, ok = QInputDialog.getText(
            self,
            "Rename Album",
            "New album name:",
            text=current_title,
        )
        if not ok:
            return
        target_name = name.strip()
        if not target_name:
            QMessageBox.warning(self, "iPhoto", "Album name cannot be empty.")
            return
        try:
            self._library.rename_album(item.album, target_name)
        except LibraryError as exc:  # pragma: no cover - GUI feedback
            QMessageBox.warning(self, "iPhoto", str(exc))
            return
        self._pending_selection = item.album.path.parent / target_name

    def _reveal_path(self, album: Optional[AlbumNode]) -> None:
        if album is None:
            return
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(album.path)))

    def _find_static_index(self, title: str) -> QModelIndex:
        root_index = self._model.index(0, 0)
        if not root_index.isValid():
            return QModelIndex()
        item = self._model.item_from_index(root_index)
        if item is None:
            return QModelIndex()
        for row in range(self._model.rowCount(root_index)):
            index = self._model.index(row, 0, root_index)
            child = self._model.item_from_index(index)
            if child and child.title == title:
                return index
        return QModelIndex()


__all__ = ["AlbumSidebar"]
