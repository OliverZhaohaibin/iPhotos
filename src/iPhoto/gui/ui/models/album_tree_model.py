"""Tree model exposing the library/album hierarchy to the sidebar."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Iterable, Optional

from PySide6.QtCore import QAbstractItemModel, QFileSystemWatcher, QModelIndex, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QFont


ALBUM_MARKERS = {".iphoto.album", ".iphoto.album.json", ".iPhoto/manifest.json"}


class NodeType(Enum):
    """Enumerate the different node semantics in the sidebar tree."""

    ROOT = auto()
    BUILTIN = auto()
    SECTION = auto()
    ALBUM = auto()
    NEW_ALBUM = auto()
    SEPARATOR = auto()


@dataclass(slots=True)
class AlbumTreeNode:
    """Internal tree node used by :class:`AlbumTreeModel`."""

    title: str
    type: NodeType
    path: Optional[Path] = None
    parent: Optional["AlbumTreeNode"] = None
    children: list["AlbumTreeNode"] = field(default_factory=list)

    def add_child(self, node: "AlbumTreeNode") -> None:
        node.parent = self
        self.children.append(node)

    def row(self) -> int:
        if self.parent is None:
            return 0
        try:
            return self.parent.children.index(self)
        except ValueError:
            return 0


class AlbumTreeModel(QAbstractItemModel):
    """Qt item model providing the album/library hierarchy."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._root_node = AlbumTreeNode("", NodeType.SECTION)
        self._library_root: Optional[Path] = None
        self._watcher = QFileSystemWatcher(self)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(250)
        self._watcher.directoryChanged.connect(self._schedule_reload)
        self._watcher.fileChanged.connect(self._schedule_reload)
        self._reload_timer.timeout.connect(self.refresh)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_library_root(self, root: Optional[Path]) -> None:
        if root is not None:
            root = root.resolve()
        if self._library_root == root:
            return
        self._library_root = root
        self.refresh()

    def library_root(self) -> Optional[Path]:
        return self._library_root

    def node_from_index(self, index: QModelIndex) -> AlbumTreeNode:
        if index.isValid():
            node = index.internalPointer()
            if isinstance(node, AlbumTreeNode):
                return node
        return self._root_node

    def refresh(self) -> None:
        self.beginResetModel()
        self._root_node = self._build_tree()
        self.endResetModel()
        self._reset_watcher()

    # ------------------------------------------------------------------
    # QAbstractItemModel implementation
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        node = self.node_from_index(parent)
        if node.type == NodeType.SEPARATOR:
            return 0
        return len(node.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 1

    def index(  # type: ignore[override]
        self, row: int, column: int, parent: QModelIndex = QModelIndex()
    ) -> QModelIndex:
        parent_node = self.node_from_index(parent)
        if not 0 <= row < len(parent_node.children):
            return QModelIndex()
        child = parent_node.children[row]
        return self.createIndex(row, column, child)

    def parent(self, index: QModelIndex) -> QModelIndex:  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        node = self.node_from_index(index)
        if node.parent is None or node.parent.parent is None:
            return QModelIndex()
        parent_node = node.parent
        return self.createIndex(parent_node.row(), 0, parent_node)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # type: ignore[override]
        if not index.isValid():
            return None
        node = self.node_from_index(index)
        if role == Qt.DisplayRole:
            return node.title
        if role == Qt.ItemDataRole.ForegroundRole and node.type == NodeType.SECTION:
            return QColor(Qt.gray)
        if role == Qt.ItemDataRole.FontRole and node.type in {NodeType.SECTION, NodeType.ROOT}:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.ForegroundRole and node.type == NodeType.NEW_ALBUM:
            return QColor(Qt.darkGreen)
        if role == Qt.ItemDataRole.ForegroundRole and node.type == NodeType.SEPARATOR:
            return QColor(Qt.lightGray)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:  # type: ignore[override]
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        node = self.node_from_index(index)
        if node.type == NodeType.SEPARATOR:
            return Qt.ItemFlag.NoItemFlags
        if node.type == NodeType.SECTION:
            return Qt.ItemFlag.ItemIsEnabled
        if node.type == NodeType.ROOT:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if node.type == NodeType.NEW_ALBUM:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_tree(self) -> AlbumTreeNode:
        container = AlbumTreeNode("", NodeType.SECTION)
        library_node = AlbumTreeNode("我的图库", NodeType.ROOT, path=self._library_root)
        container.add_child(library_node)
        self._populate_builtin_nodes(library_node)
        if self._library_root is not None:
            albums_section = AlbumTreeNode("相簿", NodeType.SECTION)
            for album_path in self._discover_albums(self._library_root):
                title = album_path.name
                albums_section.add_child(AlbumTreeNode(title, NodeType.ALBUM, path=album_path))
            albums_section.add_child(AlbumTreeNode("+ 新建相簿", NodeType.NEW_ALBUM))
            library_node.add_child(albums_section)
            library_node.add_child(
                AlbumTreeNode(
                    "最近删除",
                    NodeType.BUILTIN,
                    path=self._library_root / "Recently Deleted",
                )
            )
        return container

    def _populate_builtin_nodes(self, root: AlbumTreeNode) -> None:
        builtins: list[tuple[str, NodeType]] = [
            ("所有照片", NodeType.BUILTIN),
            ("视频", NodeType.BUILTIN),
            ("Live Photos", NodeType.BUILTIN),
            ("收藏", NodeType.BUILTIN),
            ("个人收藏", NodeType.BUILTIN),
            ("地点", NodeType.BUILTIN),
        ]
        for title, node_type in builtins:
            root.add_child(AlbumTreeNode(title, node_type, path=self._library_root))
        root.add_child(AlbumTreeNode("────", NodeType.SEPARATOR))

    def _discover_albums(self, root: Path) -> list[Path]:
        results: list[Path] = []
        if self._contains_album_marker(root):
            results.append(root)
        for directory in self._iter_directories(root):
            if self._contains_album_marker(directory):
                results.append(directory)
        results.sort()
        return results

    def _iter_directories(self, root: Path) -> Iterable[Path]:
        for entry in root.iterdir():
            if entry.is_dir():
                yield entry
                yield from self._iter_directories(entry)

    def _contains_album_marker(self, directory: Path) -> bool:
        for marker in ALBUM_MARKERS:
            candidate = directory / marker
            if candidate.exists():
                return True
        return False

    def _reset_watcher(self) -> None:
        if self._library_root is None:
            self._watcher.removePaths(self._watcher.directories())
            self._watcher.removePaths(self._watcher.files())
            return
        to_watch: set[str] = {str(self._library_root)}
        for directory in self._iter_directories(self._library_root):
            to_watch.add(str(directory))
        current_dirs = set(self._watcher.directories())
        current_files = set(self._watcher.files())
        self._watcher.removePaths(list(current_dirs - to_watch))
        self._watcher.removePaths(list(current_files))
        new_paths = list(to_watch - current_dirs)
        if new_paths:
            self._watcher.addPaths(new_paths)
        for marker in ALBUM_MARKERS:
            marker_path = self._library_root / marker
            if marker_path.exists():
                self._watcher.addPath(str(marker_path))

    def _schedule_reload(self, _path: str) -> None:
        if not self._reload_timer.isActive():
            self._reload_timer.start()
