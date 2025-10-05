"""Interactive asset grid with click and long-press handling."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QListView

from ....config import LONG_PRESS_THRESHOLD_MS


class AssetGrid(QListView):
    """Grid view that distinguishes between clicks and long presses."""

    itemClicked = Signal(object)
    requestPreview = Signal(object)
    previewReleased = Signal()
    previewCancelled = Signal()

    _DRAG_CANCEL_THRESHOLD = 6

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._on_long_press_timeout)
        self._pressed_index = None
        self._press_pos: Optional[QPoint] = None
        self._long_press_active = False

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _cancel_pending_long_press(self) -> None:
        self._press_timer.stop()
        self._reset_state()

    def _reset_state(self) -> None:
        self._long_press_active = False
        self._pressed_index = None
        self._press_pos = None

    def _on_long_press_timeout(self) -> None:
        if self._pressed_index is not None and self._pressed_index.isValid():
            self._long_press_active = True
            self.requestPreview.emit(self._pressed_index)

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
