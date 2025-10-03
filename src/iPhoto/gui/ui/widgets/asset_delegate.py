"""Custom delegate for drawing album grid tiles."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QStyledItemDelegate

from ..models.asset_model import Roles


class AssetGridDelegate(QStyledItemDelegate):
    """Render thumbnails in a tight, borderless grid."""

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._duration_font: Optional[QFont] = None

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
        painter.save()
        rect = option.rect
        pixmap = index.data(Qt.DecorationRole)
        painter.fillRect(rect, option.palette.color(QPalette.Base))

        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.setClipRect(rect)
            scaled = pixmap.scaled(rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            top_left = QPoint(
                rect.x() + (rect.width() - scaled.width()) // 2,
                rect.y() + (rect.height() - scaled.height()) // 2,
            )
            painter.drawPixmap(top_left, scaled)
            painter.setClipping(False)
        else:
            painter.fillRect(rect, QColor("#1b1b1b"))

        if option.state & QStyle.State_Selected:
            highlight = option.palette.color(QPalette.Highlight)
            overlay = QColor(highlight)
            overlay.setAlpha(110)
            painter.fillRect(rect, overlay)

        if index.data(Roles.IS_LIVE):
            self._draw_live_badge(painter, option, rect)

        if index.data(Roles.IS_VIDEO):
            self._draw_duration_badge(painter, option, rect, index.data(Roles.SIZE))

        painter.restore()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _draw_duration_badge(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        rect: QRect,
        size_info: object,
    ) -> None:
        duration = None
        if isinstance(size_info, dict):
            raw = size_info.get("duration")  # type: ignore[arg-type]
            if isinstance(raw, (int, float)):
                duration = max(0, float(raw))
        if duration is None:
            return
        text = self._format_duration(duration)
        if not text:
            return
        font = self._duration_font or QFont(option.font)
        font.setPointSizeF(max(9.0, option.font.pointSizeF() - 1))
        font.setBold(True)
        self._duration_font = font
        metrics = QFontMetrics(font)
        padding = 6
        height = metrics.height() + padding
        width = metrics.horizontalAdvance(text) + padding * 2
        badge_rect = QRect(
            rect.right() - width - 8,
            rect.bottom() - height - 8,
            width,
            height,
        )
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawRoundedRect(badge_rect, 6, 6)
        painter.setPen(QColor("white"))
        painter.setFont(font)
        painter.drawText(badge_rect, Qt.AlignCenter, text)
        painter.restore()

    def _draw_live_badge(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        rect: QRect,
    ) -> None:
        font = self._duration_font or QFont(option.font)
        font.setPointSizeF(max(8.0, option.font.pointSizeF() - 2))
        font.setBold(True)
        metrics = QFontMetrics(font)
        label = "LIVE"
        padding = 5
        height = metrics.height() + padding
        width = metrics.horizontalAdvance(label) + padding * 2
        badge_rect = QRect(rect.left() + 8, rect.top() + 8, width, height)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawRoundedRect(badge_rect, 6, 6)
        painter.setPen(QColor("white"))
        painter.setFont(font)
        painter.drawText(badge_rect, Qt.AlignCenter, label)
        painter.restore()

    @staticmethod
    def _format_duration(duration: float) -> str:
        seconds = int(round(duration))
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:d}:{secs:02d}"
