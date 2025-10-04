"""Qt item model exposing the Basic Library tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, Qt

from ....library.manager import LibraryManager
from ....library.tree import AlbumNode


class AlbumTreeRole(int, Enum):
    """Custom roles exposed by :class:`AlbumTreeModel`."""

    NODE_TYPE = Qt.ItemDataRole.UserRole + 1
    FILE_PATH = Qt.ItemDataRole.UserRole + 2
    ALBUM_NODE = Qt.ItemDataRole.UserRole + 3


class NodeType(Enum):
    """Types of nodes available in the sidebar tree."""

    ROOT = auto()
    HEADER = auto()
    SECTION = auto()
    STATIC = auto()
    ACTION = auto()
    ALBUM = auto()
    SUBALBUM = auto()
    SEPARATOR = auto()


@dataclass(slots=True)
class AlbumTreeItem:
    """Internal tree item used to back the Qt model."""

    title: str
    node_type: NodeType
    album: Optional[AlbumNode] = None
    parent: Optional["AlbumTreeItem"] = None
    children: List["AlbumTreeItem"] = field(default_factory=list)

    def add_child(self, item: "AlbumTreeItem") -> None:
        item.parent = self
        self.children.append(item)

    def child(self, index: int) -> Optional["AlbumTreeItem"]:
        if 0 <= index < len(self.children):
            return self.children[index]
        return None

    def row(self) -> int:
        if self.parent is None:
            return 0
        try:
            return self.parent.children.index(self)
        except ValueError:
            return 0


class AlbumTreeModel(QAbstractItemModel):
    """Tree model describing the Basic Library hierarchy."""

    STATIC_NODES: tuple[str, ...] = (
        "All Photos",
        "Videos",
        "Live Photos",
        "Favorites",
    )

    TRAILING_STATIC_NODES: tuple[str, ...] = ("Recently Deleted",)

    def __init__(self, library: LibraryManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self._root_item = AlbumTreeItem("root", NodeType.ROOT)
        self._path_map: Dict[Path, AlbumTreeItem] = {}
        self._library.treeUpdated.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------
    # QAbstractItemModel API
    # ------------------------------------------------------------------
    def columnCount(self, _parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 1

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        item = self._item_from_index(parent)
        return len(item.children)

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()):  # noqa: N802
        if column != 0:
            return QModelIndex()
        parent_item = self._item_from_index(parent)
        child = parent_item.child(row)
        if child is None:
            return QModelIndex()
        return self.createIndex(row, column, child)

    def parent(self, index: QModelIndex) -> QModelIndex:  # noqa: N802
        if not index.isValid():
            return QModelIndex()
        item = self._item_from_index(index)
        if item.parent is None or item.parent is self._root_item:
            return QModelIndex()
        return self.createIndex(item.parent.row(), 0, item.parent)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if not index.isValid():
            return None
        item = self._item_from_index(index)
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == Qt.ItemDataRole.ToolTipRole and item.album is not None:
            return str(item.album.path)
        if role == AlbumTreeRole.NODE_TYPE:
            return item.node_type
        if role == AlbumTreeRole.ALBUM_NODE:
            return item.album
        if role == AlbumTreeRole.FILE_PATH and item.album is not None:
            return item.album.path
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        item = self._item_from_index(index)
        if item.node_type in {NodeType.SECTION, NodeType.SEPARATOR}:
            return Qt.ItemFlag.ItemIsEnabled
        if item.node_type == NodeType.HEADER:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if item.node_type == NodeType.ACTION:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the model from the current state of the library."""

        self.beginResetModel()
        self._root_item = AlbumTreeItem("root", NodeType.ROOT)
        self._path_map.clear()
        library_root = self._library.root()
        if library_root is None:
            placeholder = AlbumTreeItem("Bind Basic Library…", NodeType.ACTION)
            self._root_item.add_child(placeholder)
            self.endResetModel()
            return

        header = AlbumTreeItem("📚 Basic Library", NodeType.HEADER)
        self._root_item.add_child(header)
        self._add_static_nodes(header)
        albums_section = AlbumTreeItem("Albums", NodeType.SECTION)
        header.add_child(albums_section)
        for album in self._library.list_albums():
            album_item = self._create_album_item(album, NodeType.ALBUM)
            albums_section.add_child(album_item)
            for child in self._library.list_children(album):
                child_item = self._create_album_item(child, NodeType.SUBALBUM)
                album_item.add_child(child_item)
        self._add_trailing_static_nodes(header)
        self.endResetModel()

    def index_for_path(self, path: Path) -> QModelIndex:
        """Return the model index associated with *path*, if any."""

        item = self._path_map.get(path) or self._path_map.get(path.resolve())
        if item is None:
            return QModelIndex()
        return self.createIndex(item.row(), 0, item)

    def item_from_index(self, index: QModelIndex) -> AlbumTreeItem | None:
        """Expose the internal item for testing and helper widgets."""

        if not index.isValid():
            return None
        return self._item_from_index(index)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _item_from_index(self, index: QModelIndex) -> AlbumTreeItem:
        if index.isValid():
            item = index.internalPointer()
            if isinstance(item, AlbumTreeItem):
                return item
        return self._root_item

    def _add_static_nodes(self, header: AlbumTreeItem) -> None:
        for title in self.STATIC_NODES:
            header.add_child(AlbumTreeItem(title, NodeType.STATIC))
        if self.STATIC_NODES:
            header.add_child(AlbumTreeItem("──────────", NodeType.SEPARATOR))

    def _add_trailing_static_nodes(self, header: AlbumTreeItem) -> None:
        if self.TRAILING_STATIC_NODES:
            header.add_child(AlbumTreeItem("──────────", NodeType.SEPARATOR))
        for title in self.TRAILING_STATIC_NODES:
            header.add_child(AlbumTreeItem(title, NodeType.STATIC))

    def _create_album_item(self, album: AlbumNode, node_type: NodeType) -> AlbumTreeItem:
        item = AlbumTreeItem(album.title, node_type, album=album)
        self._path_map[album.path] = item
        self._path_map[album.path.resolve()] = item
        return item


__all__ = ["AlbumTreeModel", "AlbumTreeItem", "NodeType", "AlbumTreeRole"]
