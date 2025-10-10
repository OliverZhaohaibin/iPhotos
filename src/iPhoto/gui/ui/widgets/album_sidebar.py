"""Sidebar widget presenting the Basic Library tree."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QCursor, QFont, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QSizePolicy,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ....library.manager import LibraryManager
from ..models.album_tree_model import AlbumTreeModel, NodeType
from ..delegates.album_sidebar_delegate import (
    AlbumSidebarDelegate,
    BranchIndicatorController,
    BG_COLOR,
    TEXT_COLOR,
    LEFT_PADDING,
    INDENT_PER_LEVEL,
    INDICATOR_SIZE,
)
from ..menus.album_sidebar_menu import show_context_menu


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
        palette.setColor(QPalette.ColorRole.Window, BG_COLOR)
        palette.setColor(QPalette.ColorRole.Base, BG_COLOR)
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

        self._tree = QTreeView()
        self._tree.setObjectName("albumSidebarTree")
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._tree.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._tree.doubleClicked.connect(self._on_double_clicked)
        self._tree.clicked.connect(self._on_clicked)
        self._tree.setMinimumWidth(220)
        self._tree.setIndentation(0)
        self._tree.setIconSize(QSize(18, 18))
        self._tree.setMouseTracking(True)
        self._tree.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._tree.setItemDelegate(AlbumSidebarDelegate(self._tree))
        self._indicator_controller = BranchIndicatorController(self._tree)
        self._tree.branch_indicator_controller = self._indicator_controller
        self._tree.setFrameShape(QFrame.Shape.NoFrame)
        self._tree.setAlternatingRowColors(False)
        self._tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tree_palette = self._tree.palette()
        tree_palette.setColor(QPalette.ColorRole.Base, BG_COLOR)
        tree_palette.setColor(QPalette.ColorRole.Window, BG_COLOR)
        tree_palette.setColor(QPalette.ColorRole.Highlight, Qt.GlobalColor.transparent)
        tree_palette.setColor(QPalette.ColorRole.HighlightedText, TEXT_COLOR)
        self._tree.setPalette(tree_palette)
        self._tree.setAutoFillBackground(True)
        self._tree.setStyleSheet(
            "QTreeView { background: transparent; border: none; }"
            "QTreeView::item { border: 0px; padding: 0px; margin: 0px; }"
            "QTreeView::branch { image: none; }"
        )

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

    def _on_clicked(self, index: QModelIndex) -> None:
        """Toggle expansion when the branch indicator hot zone is clicked."""

        if not index.isValid() or not self._model.hasChildren(index):
            return

        delegate = self._tree.itemDelegate()
        if not isinstance(delegate, AlbumSidebarDelegate):
            return

        item_rect = self._tree.visualRect(index)
        if not item_rect.isValid():
            return

        depth = delegate._depth_for_index(index)
        indentation = depth * INDENT_PER_LEVEL
        indicator_left = item_rect.left() + LEFT_PADDING + indentation
        indicator_rect = QRect(
            indicator_left,
            item_rect.top() + (item_rect.height() - INDICATOR_SIZE) // 2,
            INDICATOR_SIZE,
            INDICATOR_SIZE,
        )

        hot_zone = indicator_rect.adjusted(-4, -4, 4, 4)
        cursor_pos = QCursor.pos()
        viewport_pos = self._tree.viewport().mapFromGlobal(cursor_pos)
        if not hot_zone.contains(viewport_pos):
            return

        if self._tree.isExpanded(index):
            self._tree.collapse(index)
        else:
            self._tree.expand(index)

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
        show_context_menu(
            parent=self,
            point=point,
            tree=self._tree,
            model=self._model,
            library=self._library,
            set_pending_selection=self._set_pending_selection,
            on_bind_library=self.bindLibraryRequested.emit,
        )

    def _set_pending_selection(self, target: Path | None) -> None:
        self._pending_selection = target

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
