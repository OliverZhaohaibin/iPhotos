"""Sidebar widget presenting the Basic Library tree."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QModelIndex,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
    QVariantAnimation,
    QPersistentModelIndex,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ....errors import LibraryError
from ....library.manager import LibraryManager
from ....library.tree import AlbumNode
from ..models.album_tree_model import AlbumTreeModel, AlbumTreeRole, NodeType

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
LEFT_PADDING = 14
ICON_TEXT_GAP = 10
INDENT_PER_LEVEL = 22
INDICATOR_SLOT_WIDTH = 22
INDICATOR_SIZE = 16

@dataclass(slots=True)
class _IndicatorState:
    """Track the rendering state for a branch indicator."""

    angle: float = 0.0
    animation: QVariantAnimation | None = None


class BranchIndicatorController(QObject):
    """Animate branch indicators in sync with the tree view state."""

    def __init__(self, tree: QTreeView) -> None:
        super().__init__(tree)
        self._tree = tree
        self._states: dict[QPersistentModelIndex, _IndicatorState] = {}
        self._duration = 180

        self._tree.expanded.connect(self._on_expanded)
        self._tree.collapsed.connect(self._on_collapsed)

        model = tree.model()
        if model is not None:
            model.modelAboutToBeReset.connect(self._clear_states)

    def angle_for_index(self, index: QModelIndex) -> float:
        """Return the current angle associated with *index*."""

        self._cleanup_invalid_states()
        if not index.isValid():
            return 0.0
        key = QPersistentModelIndex(index)
        state = self._states.get(key)
        if state is None:
            angle = 90.0 if self._tree.isExpanded(index) else 0.0
            state = _IndicatorState(angle=angle)
            self._states[key] = state
        return state.angle

    def _start_animation(self, index: QModelIndex, target_angle: float) -> None:
        self._cleanup_invalid_states()
        if not index.isValid():
            return
        key = QPersistentModelIndex(index)
        state = self._states.get(key)
        if state is None:
            state = _IndicatorState(angle=target_angle)
            self._states[key] = state

        if math.isclose(state.angle, target_angle, abs_tol=0.5):
            state.angle = target_angle
            return

        if state.animation is not None:
            state.animation.stop()

        animation = QVariantAnimation(self)
        animation.setStartValue(state.angle)
        animation.setEndValue(target_angle)
        animation.setDuration(self._duration)
        animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

        index_copy = QModelIndex(index)

        def _on_value_changed(value: float) -> None:
            state.angle = float(value)
            self._tree.viewport().update(self._tree.visualRect(index_copy))

        def _on_finished() -> None:
            state.animation = None
            state.angle = target_angle
            self._tree.viewport().update(self._tree.visualRect(index_copy))

        animation.valueChanged.connect(_on_value_changed)
        animation.finished.connect(_on_finished)
        state.animation = animation
        animation.start()

    def _on_expanded(self, index: QModelIndex) -> None:
        self._start_animation(index, 90.0)

    def _on_collapsed(self, index: QModelIndex) -> None:
        self._start_animation(index, 0.0)

    def _clear_states(self) -> None:
        for state in self._states.values():
            if state.animation is not None:
                state.animation.stop()
        self._states.clear()

    def _cleanup_invalid_states(self) -> None:
        invalid = [key for key in self._states.keys() if not key.isValid()]
        for key in invalid:
            state = self._states.pop(key)
            if state.animation is not None:
                state.animation.stop()


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

        tree_view: QTreeView | None = None
        if isinstance(option.widget, QTreeView):
            tree_view = option.widget
        elif isinstance(self.parent(), QTreeView):
            tree_view = self.parent()

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

        highlight_color = None
        if node_type not in {NodeType.SECTION, NodeType.SEPARATOR}:
            if is_selected:
                highlight_color = SELECT_BG
            elif is_hover and is_enabled:
                highlight_color = HOVER_BG

        if highlight_color is not None:
            background_rect = rect.adjusted(6, 4, -6, -4)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(highlight_color)
            painter.drawRoundedRect(background_rect, ROW_RADIUS, ROW_RADIUS)

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

        text_color = TEXT_COLOR if is_enabled else DISABLED_TEXT
        if node_type == NodeType.SECTION:
            text_color = SECTION_TEXT
        elif node_type == NodeType.ACTION:
            text_color = ICON_COLOR

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        icon = index.data(Qt.ItemDataRole.DecorationRole)

        depth = self._depth_for_index(index)
        indentation = depth * INDENT_PER_LEVEL
        x = rect.left() + LEFT_PADDING + indentation

        model = index.model()
        has_children = bool(model is not None and model.hasChildren(index))

        if tree_view is not None and has_children:
            branch_rect = QRect(
                x,
                rect.top() + (rect.height() - INDICATOR_SIZE) // 2,
                INDICATOR_SIZE,
                INDICATOR_SIZE,
            )

            controller = getattr(tree_view, "branch_indicator_controller", None)
            angle = (
                controller.angle_for_index(index)
                if controller is not None
                else (90.0 if tree_view.isExpanded(index) else 0.0)
            )

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            indicator_color = TEXT_COLOR if is_enabled else DISABLED_TEXT
            pen = QPen(indicator_color)
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)

            painter.translate(branch_rect.center())
            painter.rotate(angle)

            path = QPainterPath()
            path.moveTo(-2, -4)
            path.lineTo(2, 0)
            path.lineTo(-2, 4)
            painter.drawPath(path)

            painter.restore()

            x = branch_rect.right() + 6
        elif depth > 0:
            x += INDICATOR_SLOT_WIDTH

        if icon is not None and not icon.isNull():
            icon_size = 18
            icon_rect = QRect(
                x,
                rect.top() + (rect.height() - icon_size) // 2,
                icon_size,
                icon_size,
            )
            icon.paint(
                painter,
                icon_rect,
                Qt.AlignmentFlag.AlignCenter,
            )
            x = icon_rect.right() + ICON_TEXT_GAP

        painter.setPen(text_color)
        metrics = QFontMetrics(font)
        text_rect = rect.adjusted(x - rect.left(), 0, -8, 0)
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided,
        )

        painter.restore()

    def indicator_rect(self, option: QStyleOptionViewItem, index: QModelIndex) -> QRect:
        """Return the rectangle reserved for the branch indicator."""

        rect = option.rect
        model = index.model()
        if model is None or not model.hasChildren(index):
            return QRect()

        depth = self._depth_for_index(index)
        indentation = depth * INDENT_PER_LEVEL
        x = rect.left() + LEFT_PADDING + indentation

        return QRect(
            x,
            rect.top() + (rect.height() - INDICATOR_SIZE) // 2,
            INDICATOR_SIZE,
            INDICATOR_SIZE,
        )

    @staticmethod
    def _depth_for_index(index: QModelIndex) -> int:
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        return depth


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
        tree_palette.setColor(QPalette.ColorRole.Highlight, QColor(Qt.GlobalColor.transparent))
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

        option = self._tree.viewOptions()
        option.rect = item_rect

        hot_zone = delegate.indicator_rect(option, index).adjusted(-4, -4, 4, 4)
        if hot_zone.isNull():
            return

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
