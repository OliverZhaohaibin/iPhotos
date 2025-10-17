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
        # ``QListView`` may tweak the spacing when operating in ``Adjust`` mode.
        # Record the intended gap so we can reapply it on every resize event.
        self._base_spacing = 6
        self.setSpacing(self._base_spacing)
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
        # Provide an initial grid size that roughly matches the base icon size.
        # The resize handler will immediately refine this value when the widget
        # receives its first resize event.
        self.setGridSize(
            self._base_icon_size + QSize(self._base_spacing, self._base_spacing)
        )

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        """Resize icons so they always fill the available horizontal space."""
        viewport_width = self.viewport().width()
        spacing = self._base_spacing

        # Abort early if the viewport is not ready yet. A zero width can happen
        # during initialization when Qt performs the first layout pass.
        if viewport_width <= 0:
            super().resizeEvent(event)
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
        available_width = viewport_width - (num_columns - 1) * spacing
        ideal_icon_width = available_width / num_columns

        # Use rounded icon dimensions for a smoother visual progression while
        # ensuring the total width of all columns never exceeds the viewport.
        new_dimension = max(1, int(round(ideal_icon_width)))
        while ((new_dimension + spacing) * num_columns) - spacing > viewport_width:
            if new_dimension <= 1:
                new_dimension = 1
                break
            new_dimension -= 1
        new_size = QSize(new_dimension, new_dimension)

        # Update icon and grid dimensions only when they actually change. This
        # avoids redundant relayouts and prevents feedback loops between the
        # icon size and the QListView internals.
        if self.iconSize() != new_size:
            self.setIconSize(new_size)
            self.setGridSize(new_size + QSize(spacing, spacing))
        elif self.gridSize() != new_size + QSize(spacing, spacing):
            # Keep the grid cell tightly aligned with the icon plus the fixed
            # spacing. This prevents Qt from stretching the inter-item gap when
            # reflowing the layout.
            self.setGridSize(new_size + QSize(spacing, spacing))

        # Reinstate the intended spacing in case ``Adjust`` mode attempted to
        # compensate for rounding by modifying the gap between items.
        if self.spacing() != spacing:
            self.setSpacing(spacing)

        super().resizeEvent(event)
