"""Gallery page embedding the grid view inside a simple layout."""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from .gallery_grid_view import GalleryGridView


class GalleryPageWidget(QWidget):
    """Thin wrapper that exposes the gallery grid view as a self-contained page."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("galleryPage")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.grid_view = GalleryGridView()
        layout.addWidget(self.grid_view)


__all__ = ["GalleryPageWidget"]
