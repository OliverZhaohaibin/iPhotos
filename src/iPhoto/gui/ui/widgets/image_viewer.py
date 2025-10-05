"""Widget that displays a scaled image while preserving aspect ratio."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from .live_badge import LiveBadge


class ImageViewer(QWidget):
    """Simple viewer that centers and scales a ``QPixmap``."""

    replayRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setStyleSheet("background-color: black;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)

        self._live_badge = LiveBadge(self)
        self._live_badge.hide()
        self._live_badge.move(15, 15)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        """Display *pixmap* and update the scaled rendering."""

        self._pixmap = pixmap
        self._update_pixmap()

    def clear(self) -> None:
        """Remove any currently displayed image."""

        self._pixmap = None
        self._label.clear()

    def show_live_badge(self, visible: bool) -> None:
        """Toggle visibility of the Live Photo indicator."""

        self._live_badge.setVisible(visible)
        if visible:
            self._live_badge.raise_()

    def live_badge_visible(self) -> bool:
        """Return whether the Live Photo indicator is currently visible."""

        return self._live_badge.isVisible()

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._pixmap is not None:
            self._update_pixmap()
        self._live_badge.move(15, 15)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # pragma: no cover - GUI behaviour
        if self._live_badge.isVisible() and event.button() == Qt.MouseButton.LeftButton:
            self.replayRequested.emit()
        super().mousePressEvent(event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _update_pixmap(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            self._label.clear()
            return
        target_size = self._label.size()
        if not target_size.isValid() or target_size.isEmpty():
            self._label.setPixmap(self._pixmap)
            return
        scaled = self._pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
