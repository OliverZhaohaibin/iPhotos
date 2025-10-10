"""Pre-configured asset grid for the filmstrip view."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QListView, QSizePolicy, QStyleOptionViewItem

from .asset_grid import AssetGrid
from ..models.asset_model import Roles


class FilmstripView(AssetGrid):
    """Horizontal filmstrip configured for quick navigation."""

    nextItemRequested = Signal()
    prevItemRequested = Signal()

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._base_height = 120
        self._spacing = 2
        self._default_ratio = 0.6
        icon_size = QSize(self._base_height, self._base_height)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setIconSize(icon_size)
        self.setSpacing(self._spacing)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setWordWrap(False)
        self.setStyleSheet(
            "QListView { border: none; background-color: transparent; }"
            "QListView::item { border: none; padding: 0px; margin: 0px; }"
        )
        strip_height = self._base_height + 12
        self.setMinimumHeight(strip_height)
        self.setMaximumHeight(strip_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setContentsMargins(0, 0, 0, 0)
        QTimer.singleShot(0, self._update_margins)

    def setModel(self, model) -> None:  # type: ignore[override]
        super().setModel(model)
        QTimer.singleShot(0, self._update_margins)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_margins)

    def _update_margins(self) -> None:
        viewport = self.viewport()
        if viewport is None:
            return
        viewport_width = viewport.width()
        if viewport_width <= 0:
            return

        current_width = self._current_item_width()
        if current_width <= 0:
            return

        target_padding = max(0, (viewport_width - current_width) // 2)
        margins = self.contentsMargins()
        if margins.left() == target_padding and margins.right() == target_padding:
            return
        self.setContentsMargins(target_padding, margins.top(), target_padding, margins.bottom())

    def refresh_padding(self) -> None:
        """Public helper so controllers can request a padding recalculation."""
        self._update_margins()

    def _current_item_width(self) -> int:
        model = self.model()
        delegate = self.itemDelegate()
        if model is None or delegate is None or model.rowCount() == 0:
            return self._narrow_item_width()

        current_index = None
        # Prefer the role flag that the controller keeps in sync with playback
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            if index.isValid() and bool(index.data(Roles.IS_CURRENT)):
                current_index = index
                break

        # Fallback to the view's selection if the role is not yet updated
        if current_index is None:
            selection_model = self.selectionModel()
            if selection_model is not None:
                candidate = selection_model.currentIndex()
                if candidate.isValid():
                    current_index = candidate

        if current_index is None or not current_index.isValid():
            return self._narrow_item_width()

        option = QStyleOptionViewItem()
        option.initFrom(self)
        size = delegate.sizeHint(option, current_index)
        if size.width() > 0:
            return size.width()

        width = self._visual_width(current_index)
        if width > 0:
            return width
        return self._narrow_item_width()

    def _narrow_item_width(self) -> int:
        delegate = self.itemDelegate()
        model = self.model()
        if delegate is None or model is None or model.rowCount() == 0:
            ratio = self._delegate_ratio(delegate)
            return max(1, int(round(self._base_height * ratio)))

        option = QStyleOptionViewItem()
        option.initFrom(self)
        # Prefer any non-current item to approximate the narrow width
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            if not index.isValid():
                continue
            if bool(index.data(Roles.IS_CURRENT)):
                continue
            width = self._visual_width(index)
            if width <= 0:
                size = delegate.sizeHint(option, index)
                width = size.width()
            if width > 0:
                return width

        # Fall back to the first item or the delegate ratio if needed.
        index = model.index(0, 0)
        if index.isValid():
            width = self._visual_width(index)
            if width <= 0:
                size = delegate.sizeHint(option, index)
                width = size.width()
            if width > 0:
                return width
        ratio = self._delegate_ratio(delegate)
        return max(1, int(round(self._base_height * ratio)))

    def _visual_width(self, index) -> int:
        rect = self.visualRect(index)
        width = rect.width()
        return int(width)

    def _delegate_ratio(self, delegate) -> float:
        ratio = self._default_ratio
        candidate = getattr(delegate, "_FILMSTRIP_RATIO", None)
        if isinstance(candidate, (int, float)) and candidate > 0:
            ratio = float(candidate)
        return ratio

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

    # ------------------------------------------------------------------
    # Programmatic scrolling helpers
    # ------------------------------------------------------------------
    def center_on_index(self, index: QModelIndex) -> None:
        """Scroll the view so *index* is visually centred in the viewport."""
        if not index.isValid():
            return

        item_rect = self.visualRect(index)
        if not item_rect.isValid():
            return

        viewport_width = self.viewport().width()
        if viewport_width <= 0:
            return

        target_left = (viewport_width - item_rect.width()) / 2.0
        scroll_delta = item_rect.left() - target_left
        scrollbar = self.horizontalScrollBar()
        scrollbar.setValue(scrollbar.value() + int(scroll_delta))
