"""Reusable badge widget for Live Photo overlays."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..icons import load_icon


class LiveBadge(QWidget):
    """Mac-style Live Photo badge composed of an icon and label."""

    _TEXT = "LIVE"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Allow the parent widget to receive click events so the whole surface
        # can be treated as a replay target.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._icon: QIcon = load_icon("livephoto.svg", color="white")
        self._font = QFont(self.font())
        self._font.setBold(True)
        self._font.setPointSize(10)
        self._horizontal_padding = 12
        self._vertical_padding = 6
        self._spacing = 8
        self._icon_size = 18

        self._update_fixed_size()

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:  # type: ignore[override]
        metrics = QFontMetrics(self._font)
        text_width = metrics.horizontalAdvance(self._TEXT)
        height = max(metrics.height(), self._icon_size) + (2 * self._vertical_padding)
        width = self._icon_size + text_width + (2 * self._horizontal_padding) + self._spacing
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect()
        path = QPainterPath()
        radius = min(10.0, rect.height() / 2)
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(0, 0, 0, 150))

        icon_x = rect.left() + self._horizontal_padding
        icon_y = rect.top() + (rect.height() - self._icon_size) // 2
        icon_rect = QRect(QPoint(icon_x, icon_y), QSize(self._icon_size, self._icon_size))
        if not self._icon.isNull():
            self._icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)

        text_left = icon_rect.right() + self._spacing
        text_rect = QRect(
            text_left,
            rect.top(),
            max(0, rect.right() - text_left + 1),
            rect.height(),
        )
        painter.setPen(QColor("white"))
        painter.setFont(self._font)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._TEXT)

    def changeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange}:
            self._font = QFont(self.font())
            self._font.setBold(True)
            self._update_fixed_size()
            self.update()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _update_fixed_size(self) -> None:
        hint = self.sizeHint()
        self.setFixedSize(hint)

