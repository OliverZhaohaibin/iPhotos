"""Context menu helpers for the album sidebar widget."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QWidget, QTreeView

from ....errors import LibraryError
from ....library.manager import LibraryManager
from ....library.tree import AlbumNode
from ..models.album_tree_model import AlbumTreeItem, NodeType, AlbumTreeModel


def _apply_main_window_menu_style(menu: QMenu, anchor: Optional[QWidget]) -> None:
    """Ensure ``menu`` renders opaquely and matches the main window palette."""

    menu.setAutoFillBackground(True)
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

    stylesheet: Optional[str] = None
    main_window = anchor.window() if anchor is not None else None
    if main_window is not None:
        accessor = getattr(main_window, "menu_stylesheet", None)
        if callable(accessor):
            stylesheet = accessor()
        else:
            candidate = getattr(main_window, "_menu_stylesheet", None)
            if isinstance(candidate, str):
                stylesheet = candidate
    if isinstance(stylesheet, str) and stylesheet:
        menu.setStyleSheet(stylesheet)


class AlbumSidebarContextMenu(QMenu):
    """Context menu providing album management actions."""

    def __init__(
        self,
        parent: QWidget,
        tree: QTreeView,
        model: AlbumTreeModel,
        library: LibraryManager,
        item: AlbumTreeItem,
        set_pending_selection: Callable[[Path | None], None],
        on_bind_library: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self._tree = tree
        self._model = model
        self._library = library
        self._item = item
        self._set_pending_selection = set_pending_selection
        self._on_bind_library = on_bind_library
        # Ensure the popup renders opaquely by disabling inherited translucency and reusing the
        # palette-aware stylesheet published by the main window when available.
        _apply_main_window_menu_style(self, parent)
        self._build_menu()

    def _build_menu(self) -> None:
        if self._item.node_type in {NodeType.HEADER, NodeType.SECTION}:
            self.addAction("New Album…", self._prompt_new_album)
        if self._item.node_type == NodeType.ALBUM:
            self.addAction(
                "New Sub-Album…",
                lambda: self._prompt_new_album(self._item),
            )
            self.addAction(
                "Rename Album…",
                lambda: self._prompt_rename_album(self._item),
            )
            self.addSeparator()
            self.addAction(
                "Show in File Manager",
                lambda: self._reveal_path(self._item.album),
            )
        if self._item.node_type == NodeType.SUBALBUM:
            self.addAction(
                "Rename Album…",
                lambda: self._prompt_rename_album(self._item),
            )
            self.addSeparator()
            self.addAction(
                "Show in File Manager",
                lambda: self._reveal_path(self._item.album),
            )
        if self._item.node_type == NodeType.ACTION:
            self.addAction("Set Basic Library…", self._on_bind_library)

    def _prompt_new_album(self, parent_item: Optional[AlbumTreeItem] = None) -> None:
        base_item = parent_item
        if base_item is None:
            index = self._tree.currentIndex()
            base_item = self._model.item_from_index(index)

        if base_item is None:
            return

        name, ok = QInputDialog.getText(self.parentWidget(), "New Album", "Album name:")
        if not ok:
            return
        target_name = name.strip()
        if not target_name:
            QMessageBox.warning(self.parentWidget(), "iPhoto", "Album name cannot be empty.")
            return
        try:
            if base_item.node_type == NodeType.ALBUM and base_item.album is not None:
                node = self._library.create_subalbum(base_item.album, target_name)
            else:
                node = self._library.create_album(target_name)
        except LibraryError as exc:  # pragma: no cover - GUI feedback
            QMessageBox.warning(self.parentWidget(), "iPhoto", str(exc))
            return
        self._set_pending_selection(node.path)

    def _prompt_rename_album(self, item: AlbumTreeItem) -> None:
        if item.album is None:
            return
        current_title = item.album.title
        name, ok = QInputDialog.getText(
            self.parentWidget(),
            "Rename Album",
            "New album name:",
            text=current_title,
        )
        if not ok:
            return
        target_name = name.strip()
        if not target_name:
            QMessageBox.warning(self.parentWidget(), "iPhoto", "Album name cannot be empty.")
            return
        try:
            self._library.rename_album(item.album, target_name)
        except LibraryError as exc:  # pragma: no cover - GUI feedback
            QMessageBox.warning(self.parentWidget(), "iPhoto", str(exc))
            return
        self._set_pending_selection(item.album.path.parent / target_name)

    @staticmethod
    def _reveal_path(album: Optional[AlbumNode]) -> None:
        if album is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(album.path)))


def show_context_menu(
    parent: QWidget,
    point: QPoint,
    tree: QTreeView,
    model: AlbumTreeModel,
    library: LibraryManager,
    set_pending_selection: Callable[[Path | None], None],
    on_bind_library: Callable[[], None],
) -> None:
    """Display the context menu for the album sidebar."""

    index = tree.indexAt(point)
    global_pos = tree.viewport().mapToGlobal(point)

    if not index.isValid():
        menu = QMenu(parent)
        _apply_main_window_menu_style(menu, parent)

        menu.addAction("Set Basic Library…", on_bind_library)
        menu.exec(global_pos)
        return

    item = model.item_from_index(index)
    if item is None:
        return

    menu = AlbumSidebarContextMenu(
        parent,
        tree,
        model,
        library,
        item,
        set_pending_selection,
        on_bind_library,
    )
    if not menu.isEmpty():
        menu.exec(global_pos)


__all__ = ["AlbumSidebarContextMenu", "show_context_menu"]
