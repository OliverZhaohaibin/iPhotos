"""Custom delegate for drawing album grid tiles."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QStyledItemDelegate

from ..icons import load_icon
from ..models.asset_model import Roles


class AssetGridDelegate(QStyledItemDelegate):
    """Render thumbnails in a tight, borderless grid."""

    _FILMSTRIP_RATIO = 0.6

    def __init__(self, parent=None, *, filmstrip_mode: bool = False) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._duration_font: Optional[QFont] = None
        self._live_icon: QIcon = load_icon("livephoto.svg", color="white")
        self._filmstrip_mode = filmstrip_mode
        self._base_size = 192
        self._filmstrip_padding = 6
        self._filmstrip_border_width = 3

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # type: ignore[override]
        if not self._filmstrip_mode:
            return QSize(self._base_size, self._base_size)
        is_current = bool(index.data(Roles.IS_CURRENT))
        padding = self._filmstrip_padding
        thumb_height = self._base_size
        thumb_width = self._filmstrip_thumb_width(is_current)
        return QSize(thumb_width + padding * 2, thumb_height + padding * 2)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
        painter.save()
        cell_rect = option.rect
        is_current = self._filmstrip_mode and bool(index.data(Roles.IS_CURRENT))
        thumb_rect = cell_rect
        frame_rect: Optional[QRect] = None
        base_color = option.palette.color(QPalette.Base)

        if self._filmstrip_mode:
            padding = self._filmstrip_padding
            thumb_rect = self._filmstrip_rect(cell_rect, is_current)
            frame_rect = thumb_rect.adjusted(-padding, -padding, padding, padding)
            painter.fillRect(cell_rect, base_color)
        else:
            frame_rect = None

        pixmap = index.data(Qt.DecorationRole)

        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            scaled = pixmap.scaled(
                thumb_rect.size(),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            source = scaled.rect()
            if source.width() > thumb_rect.width():
                diff = source.width() - thumb_rect.width()
                left = diff // 2
                right = diff - left
                source.adjust(left, 0, -right, 0)
            if source.height() > thumb_rect.height():
                diff = source.height() - thumb_rect.height()
                top = diff // 2
                bottom = diff - top
                source.adjust(0, top, 0, -bottom)
            painter.drawPixmap(thumb_rect, scaled, source)
        else:
            painter.fillRect(thumb_rect, QColor("#1b1b1b"))

        if option.state & QStyle.State_Selected:
            highlight = option.palette.color(QPalette.Highlight)
            overlay = QColor(highlight)
            overlay.setAlpha(60 if is_current and self._filmstrip_mode else 110)
            painter.fillRect(thumb_rect, overlay)

        if frame_rect is not None and is_current:
            highlight = option.palette.color(QPalette.Highlight)
            pen = QPen(highlight, self._filmstrip_border_width)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            inset = self._filmstrip_border_width // 2
            adjusted = frame_rect.adjusted(inset, inset, -inset, -inset)
            painter.drawRoundedRect(adjusted, 10, 10)

        if index.data(Roles.IS_LIVE):
            self._draw_live_badge(painter, option, thumb_rect)

        if index.data(Roles.IS_VIDEO):
            self._draw_duration_badge(painter, option, thumb_rect, index.data(Roles.SIZE))

        painter.restore()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _filmstrip_rect(self, rect: QRect, is_current: bool) -> QRect:
        thumb_height = self._base_size
        thumb_width = self._filmstrip_thumb_width(is_current)
        x = rect.x() + (rect.width() - thumb_width) // 2
        y = rect.y() + (rect.height() - thumb_height) // 2
        return QRect(x, y, thumb_width, thumb_height)

    def _filmstrip_thumb_width(self, is_current: bool) -> int:
        width = self._base_size if is_current else int(self._base_size * self._FILMSTRIP_RATIO)
        return max(24, width)

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
        if self._live_icon.isNull():
            return

        padding = 6
        icon_size = 18
        badge_width = icon_size + padding * 2
        badge_height = icon_size + padding * 2
        badge_rect = QRect(rect.left() + 8, rect.top() + 8, badge_width, badge_height)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawRoundedRect(badge_rect, 6, 6)
        icon_rect = QRect(
            badge_rect.left() + padding,
            badge_rect.top() + padding,
            icon_size,
            icon_size,
        )
        self._live_icon.paint(painter, icon_rect)
        painter.restore()

    @staticmethod
    def _format_duration(duration: float) -> str:
        seconds = int(round(duration))
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:d}:{secs:02d}"
