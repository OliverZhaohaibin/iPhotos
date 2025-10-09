"""QTreeView subclass hosting the album sidebar presentation."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeView

from .album_sidebar_delegate import AlbumSidebarDelegate


class AlbumTreeView(QTreeView):
    """Configure a QTreeView dedicated to the album sidebar."""

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)

        self.setItemDelegate(AlbumSidebarDelegate(self))

        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setIndentation(18)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFrameShape(QTreeView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.setStyleSheet(
            """
            QTreeView {
                background-color: transparent;
                border: none;
            }
            QTreeView::item {
                background: transparent;
                border: none;
                padding: 0px;
                margin: 0px;
            }
            QTreeView::item:selected {
                background: transparent;
                color: black;
            }
            QTreeView::item:hover {
                background: transparent;
            }
            QTreeView::branch {
                background: transparent;
            }
            """
        )


__all__ = ["AlbumTreeView"]
