"""Pre-configured asset grid for the filmstrip view."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QListView, QSizePolicy

from .asset_grid import AssetGrid


class FilmstripView(AssetGrid):
    """Horizontal filmstrip configured for quick navigation."""

    nextItemRequested = Signal()
    prevItemRequested = Signal()

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        base_size = 120
        spacing = 2
        icon_size = QSize(base_size, base_size)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setIconSize(icon_size)
        self.setGridSize(QSize(base_size, base_size))
        self.setSpacing(spacing)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setWordWrap(False)
        self.setStyleSheet("QListView::item { margin: 0px; padding: 0px; }")
        strip_height = base_size + spacing * 2 + self.frameWidth() * 2
        self.setMinimumHeight(strip_height)
        self.setMaximumHeight(strip_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------
    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        if event.modifiers() & Qt.ControlModifier:
            super().wheelEvent(event)
            return

        model = self.model()
        if model is None or model.rowCount() == 0:
            super().wheelEvent(event)
            return

        scrollbar = self.horizontalScrollBar()
        global_pos: QPoint | None = None
        if hasattr(event, "globalPosition"):
            global_pos = event.globalPosition().toPoint()
        elif hasattr(event, "globalPos"):
            global_pos = event.globalPos()

        if global_pos is not None:
            local_pos = self.mapFromGlobal(global_pos)
            if scrollbar.isVisible() and scrollbar.geometry().contains(local_pos):
                super().wheelEvent(event)
                return
            viewport_pos = self.viewport().mapFromGlobal(global_pos)
            if not self.viewport().rect().contains(viewport_pos):
                super().wheelEvent(event)
                return

        delta = event.angleDelta()
        step = delta.y() or delta.x()
        if step == 0:
            pixel_delta = event.pixelDelta()
            step = pixel_delta.y() or pixel_delta.x()
        if step == 0:
            super().wheelEvent(event)
            return

        if step < 0:
            self.nextItemRequested.emit()
        else:
            self.prevItemRequested.emit()
        event.accept()
