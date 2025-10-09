"""Sidebar widget presenting the Basic Library tree."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QFrame,
    QSizePolicy,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ....errors import LibraryError
from ....library.manager import LibraryManager
from ....library.tree import AlbumNode
from ..models.album_tree_model import (
    AlbumTreeItem,
    AlbumTreeModel,
    AlbumTreeRole,
    NodeType,
)
from .sidebar_style import SidebarStyle

# ---------------------------------------------------------------------------
# Sidebar styling helpers
# ---------------------------------------------------------------------------

BG_COLOR = QColor("#eef3f6")
TEXT_COLOR = QColor("#2b2b2b")
ICON_COLOR = QColor("#1e73ff")
HOVER_BG = QColor(0, 0, 0, 24)
SELECT_BG = QColor(0, 0, 0, 56)
DISABLED_TEXT = QColor(0, 0, 0, 90)
SECTION_TEXT = QColor(0, 0, 0, 160)
SEPARATOR_COLOR = QColor(0, 0, 0, 40)

ROW_HEIGHT = 36
ROW_RADIUS = 10
LEFT_PADDING = 10
ICON_TEXT_GAP = 8


class AlbumSidebarDelegate(QStyledItemDelegate):
    """Custom delegate painting the sidebar with a macOS inspired style."""

    def sizeHint(  # noqa: D401 - inherited docstring
        self, option: QStyleOptionViewItem, _index: QModelIndex
    ) -> QSize:
        width = option.rect.width()
        if width <= 0:
            width = 200
        return QSize(width, ROW_HEIGHT)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        painter.save()
        rect = option.rect
        node_type = index.data(AlbumTreeRole.NODE_TYPE) or NodeType.ALBUM

        tree_view = option.widget
        indentation = 0
        if isinstance(tree_view, QTreeView):
            indentation = tree_view.indentation()
        else:
            indentation = 12

        depth = 0
        parent_index = index.parent()
        while parent_index.isValid():
            depth += 1
            parent_index = parent_index.parent()
        content_left = rect.left() + depth * indentation

        if content_left < rect.right():
            content_rect = rect.adjusted(content_left - rect.left(), 0, 0, 0)
            painter.fillRect(content_rect, option.palette.base())

        # Draw separator rows as a thin line.
        if node_type == NodeType.SEPARATOR:
            pen = QPen(SEPARATOR_COLOR)
            pen.setWidth(1)
            painter.setPen(pen)
            y = rect.center().y()
            painter.drawLine(rect.left() + LEFT_PADDING, y, rect.right() - LEFT_PADDING, y)
            painter.restore()
            return

        is_enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        highlight = None
        if is_selected:
            highlight = SELECT_BG
        elif is_hover and is_enabled:
            highlight = HOVER_BG

        if node_type in {NodeType.SECTION, NodeType.SEPARATOR}:
            highlight = None

        if highlight is not None:
            background_rect = rect.adjusted(
                (content_left - rect.left()) + 6,
                4,
                -6,
                -4,
            )
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(highlight)
            painter.drawRoundedRect(background_rect, ROW_RADIUS, ROW_RADIUS)

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        icon = index.data(Qt.ItemDataRole.DecorationRole)

        font = QFont(option.font)
        if node_type == NodeType.HEADER:
            font.setPointSizeF(font.pointSizeF() + 1.0)
            font.setBold(True)
        elif node_type == NodeType.SECTION:
            font.setPointSizeF(font.pointSizeF() - 0.5)
            font.setCapitalization(QFont.Capitalization.SmallCaps)
        if node_type == NodeType.ACTION:
            font.setItalic(True)
        painter.setFont(font)

        color = TEXT_COLOR if is_enabled else DISABLED_TEXT
        if node_type == NodeType.SECTION:
            color = SECTION_TEXT
        elif node_type == NodeType.ACTION:
            color = ICON_COLOR
        painter.setPen(color)

        x = content_left + LEFT_PADDING
        icon_size = 18
        if isinstance(tree_view, QTreeView):
            icon_size = tree_view.iconSize().width()
        if icon is not None and not icon.isNull():
            icon_rect = QRect(x, rect.top(), icon_size, rect.height())
            icon.paint(
                painter,
                icon_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter,
            )
            x += icon_size + ICON_TEXT_GAP

        metrics = QFontMetrics(font)
        text_rect = rect.adjusted(x - rect.left(), 0, -8, 0)
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

        painter.restore()


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
        self._tree.setMinimumWidth(220)
        self._tree.setIndentation(12)
        self._tree.setIconSize(QSize(16, 16))
        self._tree.setMouseTracking(True)
        self._tree.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._tree.setItemDelegate(AlbumSidebarDelegate(self._tree))
        self._tree.setFrameShape(QFrame.Shape.NoFrame)
        self._tree.setAlternatingRowColors(False)
        self._tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tree.setStyle(SidebarStyle(self._tree.style()))
        tree_palette = self._tree.palette()
        tree_palette.setColor(QPalette.ColorRole.Base, BG_COLOR)
        tree_palette.setColor(QPalette.ColorRole.Window, BG_COLOR)
        self._tree.setPalette(tree_palette)
        self._tree.setAutoFillBackground(True)
        self._tree.setStyleSheet(
            "QTreeView { background: transparent; border: none; }"
            "QTreeView::item, QTreeView::item:selected, QTreeView::item:hover { "
            "  background: transparent; border: 0; padding: 0; margin: 0; }"
            "/* Ensure the branch gutter remains transparent across all states. */"
            "QTreeView::branch, "
            "QTreeView::branch:has-children, "
            "QTreeView::branch:has-children:open, "
            "QTreeView::branch:has-children:closed, "
            "QTreeView::branch:!has-children, "
            "QTreeView::branch:adjoins-item, "
            "QTreeView::branch:selected, "
            "QTreeView::branch:hover { "
            "  background: transparent; border: none; }"
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
