"""Interactive asset grid with click, long-press, and drop handling."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QMouseEvent,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import QListView

from ....config import LONG_PRESS_THRESHOLD_MS


class AssetGrid(QListView):
    """Grid view that distinguishes between clicks and long presses."""

    itemClicked = Signal(object)
    requestPreview = Signal(object)
    previewReleased = Signal()
    previewCancelled = Signal()
    visibleRowsChanged = Signal(int, int)

    _DRAG_CANCEL_THRESHOLD = 6
    _IDEAL_ICON_SIZE = 192

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._on_long_press_timeout)
        self._pressed_index = None
        self._press_pos: Optional[QPoint] = None
        self._long_press_active = False
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(100)
        self._update_timer.timeout.connect(self._emit_visible_rows)
        self._visible_range: Optional[tuple[int, int]] = None
        self._model = None
        self._external_drop_enabled = False
        self._drop_handler: Optional[Callable[[List[Path]], None]] = None
        self._drop_validator: Optional[Callable[[List[Path]], bool]] = None

    # ------------------------------------------------------------------
    # Mouse event handling
    # ------------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            viewport_pos = self._viewport_pos(event)
            index = self.indexAt(viewport_pos)
            if index.isValid():
                self._pressed_index = index
                self._press_pos = QPoint(viewport_pos)
                self._long_press_active = False
                self._press_timer.start(LONG_PRESS_THRESHOLD_MS)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._press_pos is not None and not self._long_press_active:
            viewport_pos = self._viewport_pos(event)
            if (viewport_pos - self._press_pos).manhattanLength() > self._DRAG_CANCEL_THRESHOLD:
                self._cancel_pending_long_press()
        elif self._long_press_active and self._pressed_index is not None:
            viewport_pos = self._viewport_pos(event)
            index = self.indexAt(viewport_pos)
            if not index.isValid() or index != self._pressed_index:
                self.previewCancelled.emit()
                self._reset_state()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        was_long_press = self._long_press_active
        index = self._pressed_index
        self._cancel_pending_long_press()
        if event.button() == Qt.MouseButton.LeftButton and index is not None:
            if was_long_press:
                self.previewReleased.emit()
            elif index.isValid():
                self.itemClicked.emit(index)
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        if self._long_press_active:
            self.previewCancelled.emit()
        self._cancel_pending_long_press()
        super().leaveEvent(event)

    def showEvent(self, event: QShowEvent) -> None:  # type: ignore[override]
        """React to the grid becoming visible by synchronising the layout."""

        super().showEvent(event)
        self._update_grid_size()
        QTimer.singleShot(0, self._schedule_visible_rows_update)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        """Resize the grid while keeping thumbnails neatly aligned."""

        super().resizeEvent(event)
        self._update_grid_size()
        self._schedule_visible_rows_update()

    # ------------------------------------------------------------------
    # External file drop configuration
    # ------------------------------------------------------------------
    def configure_external_drop(
        self,
        *,
        handler: Optional[Callable[[List[Path]], None]] = None,
        validator: Optional[Callable[[List[Path]], bool]] = None,
    ) -> None:
        """Enable or disable external drop support for the grid view.

        Parameters
        ----------
        handler:
            Callable invoked when a valid drop operation completes.  When
            ``None`` the grid reverts to its default behaviour and external
            drops are ignored entirely.
        validator:
            Optional callable used to preflight an incoming drag.  The
            validator receives the list of candidate file paths and returns
            ``True`` to accept the drag or ``False`` to reject it.  When left
            unspecified every drop that provides at least one local file is
            considered acceptable.
        """

        self._drop_handler = handler
        self._drop_validator = validator
        self._external_drop_enabled = handler is not None
        self.setAcceptDrops(self._external_drop_enabled)
        # The visual items live inside the viewport object, so we must enable drops there
        # as well; otherwise Qt will refuse to deliver drag/drop events to the grid.
        self.viewport().setAcceptDrops(self._external_drop_enabled)

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # type: ignore[override]
        super().scrollContentsBy(dx, dy)
        self._schedule_visible_rows_update()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # type: ignore[override]
        if not self._external_drop_enabled:
            super().dragEnterEvent(event)
            return
        paths = self._extract_local_files(event)
        if not paths:
            event.ignore()
            return
        if self._drop_validator is not None and not self._drop_validator(paths):
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # type: ignore[override]
        if not self._external_drop_enabled:
            super().dragMoveEvent(event)
            return
        paths = self._extract_local_files(event)
        if not paths:
            event.ignore()
            return
        if self._drop_validator is not None and not self._drop_validator(paths):
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:  # type: ignore[override]
        if not self._external_drop_enabled or self._drop_handler is None:
            super().dropEvent(event)
            return
        paths = self._extract_local_files(event)
        if not paths:
            event.ignore()
            return
        if self._drop_validator is not None and not self._drop_validator(paths):
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self._drop_handler(paths)

    def setModel(self, model) -> None:  # type: ignore[override]
        if self._model is not None:
            try:
                self._model.modelReset.disconnect(self._schedule_visible_rows_update)
            except (RuntimeError, TypeError):
                pass
            try:
                self._model.rowsInserted.disconnect(self._schedule_visible_rows_update)
            except (RuntimeError, TypeError):
                pass
            try:
                self._model.rowsRemoved.disconnect(self._schedule_visible_rows_update)
            except (RuntimeError, TypeError):
                pass
        super().setModel(model)
        self._model = model
        if model is not None:
            model.modelReset.connect(self._schedule_visible_rows_update)
            model.rowsInserted.connect(self._schedule_visible_rows_update)
            model.rowsRemoved.connect(self._schedule_visible_rows_update)
        self._schedule_visible_rows_update()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _cancel_pending_long_press(self) -> None:
        self._press_timer.stop()
        self._reset_state()

    def _update_grid_size(self) -> None:
        """Scale icon geometry to fill the available horizontal space.

        The method computes how many square thumbnails can comfortably fit across
        the viewport using :attr:`_IDEAL_ICON_SIZE` as the preferred baseline.
        When the window grows large enough for an additional column we increase
        the column count and shrink the individual icons slightly so the row is
        filled edge-to-edge without leaving trailing gaps.
        """

        if self.viewMode() != QListView.ViewMode.IconMode:
            # List mode delegates sizing to the item delegate so we do not apply
            # any custom geometry adjustments here.
            return

        # A small padding compensates for the viewport's frame border, which
        # helps prevent horizontal scrollbars from flickering during resize.
        width = self.viewport().width() - 5
        if width <= 0:
            return

        spacing = self.spacing()
        if spacing < 0:  # pragma: no cover - Qt occasionally reports -1
            spacing = 0

        # Determine the maximum number of columns that still respects our ideal
        # thumbnail size.  We always keep at least one column so the view never
        # collapses to zero when the window becomes very narrow.
        numerator = width + spacing
        denominator = self._IDEAL_ICON_SIZE + spacing
        num_columns = max(1, math.floor(numerator / denominator))

        # Reconstruct the exact icon width that will consume the entire row once
        # we account for the inter-item spacing.  The icons are kept square to
        # match the image thumbnails produced by the rest of the application.
        available_width = width - (num_columns - 1) * spacing
        new_icon_width = math.floor(available_width / num_columns)

        if new_icon_width <= 0:
            return

        new_size = QSize(new_icon_width, new_icon_width)
        if new_size == self.iconSize():
            return

        self.setIconSize(new_size)
        # ``gridSize`` includes the icon as well as the small decorative border
        # drawn by the delegate; keeping this offset minimal avoids visual gaps.
        self.setGridSize(QSize(new_icon_width + 2, new_icon_width + 2))
        self._schedule_visible_rows_update()

    def _reset_state(self) -> None:
        self._long_press_active = False
        self._pressed_index = None
        self._press_pos = None

    def _on_long_press_timeout(self) -> None:
        if self._pressed_index is not None and self._pressed_index.isValid():
            self._long_press_active = True
            self.requestPreview.emit(self._pressed_index)

    def _schedule_visible_rows_update(self) -> None:
        self._update_timer.start()

    def _viewport_pos(self, event: QMouseEvent) -> QPoint:
        """Return the event position mapped into viewport coordinates."""

        viewport = self.viewport()

        def _validated(point: Optional[QPoint]) -> Optional[QPoint]:
            if point is None:
                return None
            if viewport.rect().contains(point):
                return point
            return None

        if hasattr(event, "position"):
            candidate = _validated(event.position().toPoint())
            if candidate is not None:
                return candidate

        if hasattr(event, "pos"):
            candidate = _validated(event.pos())
            if candidate is not None:
                return candidate

        global_point: Optional[QPoint] = None

        global_position = getattr(event, "globalPosition", None)
        if callable(global_position):
            global_point = global_position().toPoint()
        elif global_position is not None:
            global_point = global_position.toPoint()

        if global_point is None and hasattr(event, "globalPos"):
            global_point = event.globalPos()

        if global_point is not None:
            mapped = viewport.mapFromGlobal(global_point)
            candidate = _validated(mapped)
            if candidate is not None:
                return candidate

        # Fallback for any other exotic QMouseEvent implementations. At this point
        # we have no reliable coordinate system information, so best-effort return
        # of the event's integer components is the safest option.
        return QPoint(event.x(), event.y())

    def _emit_visible_rows(self) -> None:
        model = self.model()
        if model is None:
            return
        row_count = model.rowCount()
        if row_count == 0:
            if self._visible_range is not None:
                self._visible_range = None
            return
        viewport_rect = self.viewport().rect()
        if viewport_rect.isEmpty():
            return

        top_index = self.indexAt(viewport_rect.topLeft())
        bottom_index = self.indexAt(viewport_rect.bottomRight())

        first = top_index.row()
        last = bottom_index.row()

        if first == -1 and last == -1:
            return
        if first == -1:
            first = 0
        if last == -1:
            last = row_count - 1

        buffer = 20
        first = max(0, first - buffer)
        last = min(row_count - 1, last + buffer)
        if first > last:
            return

        visible_range = (first, last)
        if self._visible_range == visible_range:
            return

        self._visible_range = visible_range
        self.visibleRowsChanged.emit(first, last)

    def _extract_local_files(self, event: QDropEvent | QDragEnterEvent | QDragMoveEvent) -> List[Path]:
        """Return all unique local file paths advertised by *event*.

        The helper normalises the reported URLs, discards remote resources, and
        guarantees deterministic ordering so validators can rely on stable
        inputs.  ``Path.resolve`` is intentionally avoided here to keep the
        method lightweight; callers that require canonical paths can resolve
        them as needed.
        """

        mime = event.mimeData()
        if mime is None:
            return []
        urls = getattr(mime, "urls", None)
        if not callable(urls):
            return []
        seen: set[Path] = set()
        paths: List[Path] = []
        for url in urls():
            if not url.isLocalFile():
                continue
            local = Path(url.toLocalFile()).expanduser()
            if local in seen:
                continue
            seen.add(local)
            paths.append(local)
        return paths
