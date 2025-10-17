"""Pre-configured grid view for the gallery layout."""

from __future__ import annotations

import math

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QAbstractItemView, QListView

from .asset_grid import AssetGrid


class GalleryGridView(AssetGrid):
    """Dense icon-mode grid tuned for album browsing."""

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._base_icon_size = QSize(192, 192)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setIconSize(self._base_icon_size)
        self.setGridSize(self._base_icon_size)
        self.setSpacing(6)
        self.setUniformItemSizes(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setWordWrap(False)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        """Resize icons so they always fill the available horizontal space."""
        super().resizeEvent(event)

        viewport_width = self.viewport().width()
        spacing = self.spacing()

        # Abort early if the viewport is not ready yet. A zero width can happen
        # during initialization when Qt performs the first layout pass.
        if viewport_width <= 0:
            return

        # Determine how many columns *should* be visible for the current width.
        # The baseline size provides a pleasant default item dimension that we
        # scale up or down depending on the window width. Adding ``spacing`` to
        # the numerator ensures that the final column is not prematurely
        # truncated when the viewport width aligns exactly with a multiple of
        # ``base_size + spacing``.
        base_width = self._base_icon_size.width()
        num_columns = max(1, math.floor((viewport_width + spacing) / (base_width + spacing)))

        # Compute the exact icon width so that ``num_columns`` tiles completely
        # fill the viewport. The grid uses uniform spacing, therefore we only
        # need to remove the total spacing footprint and divide the remainder
        # by the number of columns. Icons stay square to avoid letterboxing.
        new_icon_width = (viewport_width - (num_columns - 1) * spacing) / num_columns
        new_dimension = max(1, int(new_icon_width))
        new_size = QSize(new_dimension, new_dimension)

        # Update icon and grid dimensions only when they actually change. This
        # avoids redundant relayouts and prevents feedback loops between the
        # icon size and the QListView internals.
        if self.iconSize() != new_size:
            self.setIconSize(new_size)
            self.setGridSize(new_size)
