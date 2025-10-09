"""Custom item delegate rendering the album sidebar with macOS inspired styling."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QStyledItemDelegate

from ..models.album_tree_model import AlbumTreeRole, NodeType

# ---------------------------------------------------------------------------
# Sidebar styling constants
# ---------------------------------------------------------------------------

TEXT_COLOR = QColor("#1d1d1f")
SECTION_TEXT_COLOR = QColor("#6e6e73")
HOVER_BG_COLOR = QColor(0, 0, 0, 24)
SELECT_BG_COLOR = QColor("#d1d1d6")
ICON_COLOR = QColor("#6e6e73")
ICON_SELECTED_COLOR = QColor("#007aff")
DISABLED_ALPHA = 120

ROW_HEIGHT = 32
ROW_RADIUS = 6
LEFT_PADDING = 14
ICON_TEXT_GAP = 8
ICON_SIZE = 18
BACKGROUND_MARGIN = (6, 4, -6, -4)


class AlbumSidebarDelegate(QStyledItemDelegate):
    """Render sidebar rows by fully controlling state-dependent painting."""

    def sizeHint(self, option: QStyleOptionViewItem, _index: QModelIndex) -> QSize:  # noqa: D401
        width = option.rect.width() or 200
        return QSize(width, ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect
        node_type = index.data(AlbumTreeRole.NODE_TYPE) or NodeType.ALBUM

        if node_type == NodeType.SEPARATOR:
            pen = QPen(QColor(0, 0, 0, 40))
            pen.setWidth(1)
            painter.setPen(pen)
            y = rect.center().y()
            painter.drawLine(rect.left() + LEFT_PADDING, y, rect.right() - LEFT_PADDING, y)
            painter.restore()
            return

        is_enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        interactive_nodes = {
            NodeType.STATIC,
            NodeType.ALBUM,
            NodeType.SUBALBUM,
            NodeType.ACTION,
        }
        if node_type in interactive_nodes:
            margin_left, margin_top, margin_right, margin_bottom = BACKGROUND_MARGIN
            background_rect = rect.adjusted(
                margin_left,
                margin_top,
                margin_right,
                margin_bottom,
            )
            if is_selected:
                painter.setBrush(SELECT_BG_COLOR)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(background_rect, ROW_RADIUS, ROW_RADIUS)
            elif is_hover and is_enabled:
                painter.setBrush(HOVER_BG_COLOR)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(background_rect, ROW_RADIUS, ROW_RADIUS)

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        icon = index.data(Qt.ItemDataRole.DecorationRole)

        font = QFont(option.font)
        if node_type == NodeType.HEADER:
            font.setPointSizeF(font.pointSizeF() + 1.0)
            font.setBold(True)
        elif node_type == NodeType.SECTION:
            font.setPointSizeF(font.pointSizeF() - 0.5)
            font.setCapitalization(QFont.Capitalization.AllUppercase)
        if node_type == NodeType.ACTION:
            font.setItalic(True)
        painter.setFont(font)

        text_color = TEXT_COLOR
        icon_color = ICON_COLOR
        if node_type == NodeType.SECTION:
            text_color = SECTION_TEXT_COLOR
        elif node_type == NodeType.ACTION:
            text_color = ICON_SELECTED_COLOR
        if is_selected:
            icon_color = ICON_SELECTED_COLOR
        if not is_enabled:
            text_color = QColor(text_color)
            icon_color = QColor(icon_color)
            text_color.setAlpha(DISABLED_ALPHA)
            icon_color.setAlpha(DISABLED_ALPHA)

        x_offset = rect.left() + LEFT_PADDING
        if icon is not None and not icon.isNull():
            icon_rect = QRect(
                x_offset,
                rect.top() + (rect.height() - ICON_SIZE) // 2,
                ICON_SIZE,
                ICON_SIZE,
            )
            pixmap = icon.pixmap(ICON_SIZE, ICON_SIZE)
            if not pixmap.isNull():
                tinted = QPixmap(pixmap.size())
                tinted.fill(Qt.GlobalColor.transparent)
                mask_painter = QPainter(tinted)
                mask_painter.drawPixmap(0, 0, pixmap)
                mask_painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
                mask_painter.fillRect(tinted.rect(), icon_color)
                mask_painter.end()
                painter.drawPixmap(icon_rect.topLeft(), tinted)
            x_offset += ICON_SIZE + ICON_TEXT_GAP

        painter.setPen(text_color)
        metrics = QFontMetrics(font)
        text_rect = rect.adjusted(x_offset - rect.left(), 0, -8, 0)
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

        painter.restore()


__all__ = ["AlbumSidebarDelegate"]
