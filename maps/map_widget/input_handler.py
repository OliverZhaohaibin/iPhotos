"""Logic for translating Qt input events into map navigation requests."""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, Qt, Signal


class InputHandler(QObject):
    """Handle mouse interaction for :class:`~map_widget.map_widget.MapWidget`."""

    pan_requested = Signal(QPointF)
    """Signal emitted for every incremental drag delta while the user pans."""

    pan_finished = Signal()
    """Signal emitted once the active drag gesture completes."""

    zoom_requested = Signal(float, QPointF)
    cursor_changed = Signal(Qt.CursorShape)
    cursor_reset = Signal()

    def __init__(self, *, min_zoom: float, max_zoom: float, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self._is_dragging = False
        self._last_mouse_pos = QPointF()

    # ------------------------------------------------------------------
    def handle_mouse_press(self, event) -> None:
        """Start a drag gesture when the primary mouse button is pressed."""

        if event.button() == Qt.LeftButton:
            self._is_dragging = True
            self._last_mouse_pos = event.position()
            self.cursor_changed.emit(Qt.ClosedHandCursor)

    # ------------------------------------------------------------------
    def handle_mouse_move(self, event) -> None:
        """Emit a pan request when the mouse moves during a drag gesture."""

        if self._is_dragging and event.buttons() & Qt.LeftButton:
            current_pos = event.position()
            delta = current_pos - self._last_mouse_pos
            self._last_mouse_pos = current_pos
            self.pan_requested.emit(delta)

    # ------------------------------------------------------------------
    def handle_mouse_release(self, event) -> None:
        """Finish drag gestures and restore the default cursor."""

        if event.button() == Qt.LeftButton and self._is_dragging:
            # ``pan_finished`` is emitted before the cursor resets so listeners
            # can perform any final bookkeeping while the drag context is still
            # active.
            self._is_dragging = False
            self.pan_finished.emit()
            self.cursor_reset.emit()

    # ------------------------------------------------------------------
    def handle_wheel_event(self, event, current_zoom: float) -> None:
        """Request a zoom change that keeps the cursor location stationary."""

        delta = event.angleDelta().y()
        if delta == 0:
            return

        zoom_factor = 1.0 + delta / 1200.0
        new_zoom = max(self._min_zoom, min(self._max_zoom, current_zoom * zoom_factor))
        if new_zoom == current_zoom:
            return

        self.zoom_requested.emit(new_zoom, event.position())


__all__ = ["InputHandler"]
