"""Sidebar widget that renders the library/album tree."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QModelIndex, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QInputDialog,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..actions.album_actions import AlbumActionError, AlbumActions
from ..models.album_tree_model import AlbumTreeModel, NodeType


class AlbumSidebar(QWidget):
    """Tree-based navigation area listing albums and library shortcuts."""

    albumSelected = Signal(Path)
    albumCreated = Signal(Path)
    albumRemoved = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = AlbumTreeModel(self)
        self._tree = QTreeView(self)
        self._actions = AlbumActions()
        self._library_root: Optional[Path] = None
        self._configure_tree()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._tree)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_library_root(self, root: Optional[Path]) -> None:
        if root is not None:
            root = root.resolve()
        if self._library_root == root:
            return
        self._library_root = root
        self._model.set_library_root(root)
        self._expand_defaults()

    def select_album(self, album_path: Path) -> None:
        index = self._find_index_for_path(album_path.resolve())
        if index is not None:
            self._tree.setCurrentIndex(index)
            self._tree.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    def _configure_tree(self) -> None:
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        self._tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._tree.clicked.connect(self._on_item_activated)
        self._tree.doubleClicked.connect(self._on_item_activated)
        header = self._tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

    def _expand_defaults(self) -> None:
        self._tree.expandAll()

    def _show_context_menu(self, point: QPoint) -> None:
        index = self._tree.indexAt(point)
        node = self._model.node_from_index(index)
        menu = QMenu(self)
        new_action = menu.addAction("新建相簿…")
        rename_action = None
        delete_action = None
        if node.type == NodeType.ALBUM and node.path is not None:
            rename_action = menu.addAction("重命名…")
            delete_action = menu.addAction("删除相簿")
        if self._library_root is None:
            new_action.setEnabled(False)
        chosen = menu.exec(self._tree.viewport().mapToGlobal(point))
        if chosen is None:
            return
        if chosen == new_action:
            self._prompt_new_album()
            return
        if rename_action is not None and chosen == rename_action:
            self._prompt_rename_album(index)
            return
        if delete_action is not None and chosen == delete_action:
            self._prompt_delete_album(index)

    def _on_item_activated(self, index: QModelIndex) -> None:
        node = self._model.node_from_index(index)
        if node.type == NodeType.NEW_ALBUM:
            previous = self._tree.currentIndex()
            created = self._prompt_new_album()
            if not created and previous.isValid():
                self._tree.setCurrentIndex(previous)
            return
        if node.type == NodeType.ALBUM and node.path is not None:
            self.albumSelected.emit(node.path)

    def _prompt_new_album(self) -> bool:
        if self._library_root is None:
            QMessageBox.information(self, "iPhoto", "请先打开图库根目录。")
            return False
        name, ok = QInputDialog.getText(self, "新建相簿", "相簿名称：")
        if not ok or not name.strip():
            return False
        try:
            new_path = self._actions.create_album(self._library_root, name)
        except AlbumActionError as exc:
            QMessageBox.warning(self, "iPhoto", str(exc))
            return False
        self._model.refresh()
        self._expand_defaults()
        index = self._find_index_for_path(new_path)
        if index is not None:
            self._tree.setCurrentIndex(index)
        self.albumCreated.emit(new_path)
        return True

    def _prompt_rename_album(self, index: QModelIndex) -> None:
        node = self._model.node_from_index(index)
        if node.path is None:
            return
        current = node.path.name
        name, ok = QInputDialog.getText(self, "重命名相簿", "新的相簿名：", text=current)
        if not ok or not name.strip() or name == current:
            return
        try:
            new_path = self._actions.rename_album(node.path, name)
        except AlbumActionError as exc:
            QMessageBox.warning(self, "iPhoto", str(exc))
            return
        self._model.refresh()
        self._expand_defaults()
        new_index = self._find_index_for_path(new_path)
        if new_index is not None:
            self._tree.setCurrentIndex(new_index)
            self.albumSelected.emit(new_path)

    def _prompt_delete_album(self, index: QModelIndex) -> None:
        node = self._model.node_from_index(index)
        if node.path is None:
            return
        confirm = QMessageBox.question(
            self,
            "删除相簿",
            f"确定要删除“{node.path.name}”以及其中的所有文件吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._actions.delete_album(node.path)
        except AlbumActionError as exc:
            QMessageBox.warning(self, "iPhoto", str(exc))
            return
        self._model.refresh()
        self._expand_defaults()
        self.albumRemoved.emit(node.path)

    def _find_index_for_path(self, album_path: Path) -> QModelIndex | None:
        def _walk(node: QModelIndex) -> Optional[QModelIndex]:
            item = self._model.node_from_index(node)
            if item.path is not None and item.path.resolve() == album_path:
                return node
            for row in range(self._model.rowCount(node)):
                child = self._model.index(row, 0, node)
                found = _walk(child)
                if found is not None:
                    return found
            return None

        root_index = QModelIndex()
        for row in range(self._model.rowCount(root_index)):
            index = self._model.index(row, 0, root_index)
            found = _walk(index)
            if found is not None:
                return found
        return None
