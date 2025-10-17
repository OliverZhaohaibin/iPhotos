"""Custom delegate and animations for the album sidebar tree."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import (
    QEasingCurve,
    QModelIndex,
    QObject,
    QRect,
    QSize,
    Qt,
    QVariantAnimation,
    QPersistentModelIndex,
)
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QStyleOptionViewItem, QTreeView

from ..models.album_tree_model import AlbumTreeRole, NodeType
from .. import palette

ROW_HEIGHT = 36
ROW_RADIUS = 10
LEFT_PADDING = 14
ICON_TEXT_GAP = 10
INDENT_PER_LEVEL = 22
INDICATOR_SLOT_WIDTH = 22
INDICATOR_SIZE = 16
@dataclass(slots=True)
class _IndicatorState:
    """Track the rendering state for a branch indicator."""

    angle: float = 0.0
    animation: QVariantAnimation | None = None


class BranchIndicatorController(QObject):
    """Animate branch indicators in sync with the tree view state."""

    def __init__(self, tree: QTreeView) -> None:
        super().__init__(tree)
        self._tree = tree
        self._states: dict[QPersistentModelIndex, _IndicatorState] = {}
        self._duration = 180

        self._tree.expanded.connect(self._on_expanded)
        self._tree.collapsed.connect(self._on_collapsed)

        model = tree.model()
        if model is not None:
            model.modelAboutToBeReset.connect(self._clear_states)

    def angle_for_index(self, index: QModelIndex) -> float:
        """Return the current angle associated with *index*."""

        self._cleanup_invalid_states()
        if not index.isValid():
            return 0.0
        key = QPersistentModelIndex(index)
        state = self._states.get(key)
        if state is None:
            angle = 90.0 if self._tree.isExpanded(index) else 0.0
            state = _IndicatorState(angle=angle)
            self._states[key] = state
        return state.angle

    def _start_animation(self, index: QModelIndex, target_angle: float) -> None:
        self._cleanup_invalid_states()
        if not index.isValid():
            return
        key = QPersistentModelIndex(index)
        state = self._states.get(key)
        if state is None:
            state = _IndicatorState(angle=target_angle)
            self._states[key] = state

        if math.isclose(state.angle, target_angle, abs_tol=0.5):
            state.angle = target_angle
            return

        if state.animation is not None:
            state.animation.stop()

        animation = QVariantAnimation(self)
        animation.setStartValue(state.angle)
        animation.setEndValue(target_angle)
        animation.setDuration(self._duration)
        animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

        index_copy = QModelIndex(index)

        def _on_value_changed(value: float) -> None:
            state.angle = float(value)
            self._tree.viewport().update(self._tree.visualRect(index_copy))

        def _on_finished() -> None:
            state.animation = None
            state.angle = target_angle
            self._tree.viewport().update(self._tree.visualRect(index_copy))

        animation.valueChanged.connect(_on_value_changed)
        animation.finished.connect(_on_finished)
        state.animation = animation
        animation.start()

    def _on_expanded(self, index: QModelIndex) -> None:
        self._start_animation(index, 90.0)

    def _on_collapsed(self, index: QModelIndex) -> None:
        self._start_animation(index, 0.0)

    def _clear_states(self) -> None:
        for state in self._states.values():
            if state.animation is not None:
                state.animation.stop()
        self._states.clear()

    def _cleanup_invalid_states(self) -> None:
        invalid = [key for key in self._states.keys() if not key.isValid()]
        for key in invalid:
            state = self._states.pop(key)
            if state.animation is not None:
                state.animation.stop()


class AlbumSidebarDelegate(QStyledItemDelegate):
    """Custom delegate painting the sidebar with a macOS inspired style."""

    def sizeHint(  # noqa: D401 - inherited docstring
        self, option: QStyleOptionViewItem, _index: QModelIndex
    ) -> QSize:
        width = option.rect.width()
        if width <= 0:
            width = 200
        return QSize(width, ROW_HEIGHT)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        painter.save()
        rect = option.rect
        node_type = index.data(AlbumTreeRole.NODE_TYPE) or NodeType.ALBUM

        tree_view: QTreeView | None = None
        if isinstance(option.widget, QTreeView):
            tree_view = option.widget
        elif isinstance(self.parent(), QTreeView):
            tree_view = self.parent()

        if node_type == NodeType.SEPARATOR:
            pen = QPen(palette.SIDEBAR_SEPARATOR)
            pen.setWidth(1)
            painter.setPen(pen)
            y = rect.center().y()
            painter.drawLine(rect.left() + LEFT_PADDING, y, rect.right() - LEFT_PADDING, y)
            painter.restore()
            return

        is_enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        highlight_color = None
        if node_type not in {NodeType.SECTION, NodeType.SEPARATOR}:
            if is_selected:
                highlight_color = palette.SIDEBAR_SELECTION_BACKGROUND
            elif is_hover and is_enabled:
                highlight_color = palette.SIDEBAR_HOVER_BACKGROUND

        if highlight_color is not None:
            background_rect = rect.adjusted(6, 4, -6, -4)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(highlight_color)
            painter.drawRoundedRect(background_rect, ROW_RADIUS, ROW_RADIUS)

        font = QFont(option.font)
        if node_type == NodeType.HEADER:
            font.setPointSizeF(font.pointSizeF() + 1.0)
            font.setBold(True)
        elif node_type == NodeType.SECTION:
            font.setPointSizeF(font.pointSizeF() - 0.5)
            font.setCapitalization(QFont.Capitalization.SmallCaps)
        if node_type == NodeType.ACTION:
            font.setItalic(True)
        painter.setFont(font)

        text_color = palette.SIDEBAR_TEXT if is_enabled else palette.SIDEBAR_DISABLED_TEXT
        if node_type == NodeType.SECTION:
            text_color = palette.SIDEBAR_SECTION_TEXT
        elif node_type == NodeType.ACTION:
            text_color = palette.SIDEBAR_ICON_ACCENT

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        icon = index.data(Qt.ItemDataRole.DecorationRole)

        depth = self._depth_for_index(index)
        indentation = depth * INDENT_PER_LEVEL
        x = rect.left() + LEFT_PADDING + indentation

        model = index.model()
        has_children = bool(model is not None and model.hasChildren(index))

        if tree_view is not None and has_children:
            branch_rect = QRect(
                x,
                rect.top() + (rect.height() - INDICATOR_SIZE) // 2,
                INDICATOR_SIZE,
                INDICATOR_SIZE,
            )

            controller = getattr(tree_view, "branch_indicator_controller", None)
            angle = (
                controller.angle_for_index(index)
                if controller is not None
                else (90.0 if tree_view.isExpanded(index) else 0.0)
            )

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            indicator_color = (
                palette.SIDEBAR_TEXT if is_enabled else palette.SIDEBAR_DISABLED_TEXT
            )
            pen = QPen(indicator_color)
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)

            painter.translate(branch_rect.center())
            painter.rotate(angle)

            path = QPainterPath()
            path.moveTo(-2, -4)
            path.lineTo(2, 0)
            path.lineTo(-2, 4)
            painter.drawPath(path)

            painter.restore()

            x = branch_rect.right() + 6
        elif depth > 0:
            x += INDICATOR_SLOT_WIDTH

        # ------------------------------------------------------------------
        # Icon rendering
        # ------------------------------------------------------------------
        # Every icon is now pre-tinted and cached by the model, so the delegate
        # can rely on Qt's ``QIcon.paint`` helper. The helper renders scalable
        # SVG assets at the ideal resolution for the target device, which keeps
        # the glyphs tack sharp without forcing the delegate to manage explicit
        # supersampling buffers or device pixel ratios.
        if icon is not None and not icon.isNull():
            icon_size = 18
            icon_rect = QRect(
                x,
                rect.top() + (rect.height() - icon_size) // 2,
                icon_size,
                icon_size,
            )
            icon.paint(
                painter,
                icon_rect,
                Qt.AlignmentFlag.AlignCenter,
            )
            x = icon_rect.right() + ICON_TEXT_GAP

        painter.setPen(text_color)
        metrics = QFontMetrics(font)
        text_rect = rect.adjusted(x - rect.left(), 0, -8, 0)
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided,
        )

        painter.restore()

    @staticmethod
    def _depth_for_index(index: QModelIndex) -> int:
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        return depth


__all__ = [
    "AlbumSidebarDelegate",
    "BranchIndicatorController",
    "LEFT_PADDING",
    "INDENT_PER_LEVEL",
    "INDICATOR_SIZE",
]
