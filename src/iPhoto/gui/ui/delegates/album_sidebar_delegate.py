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
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QTreeView, QStyle

from ..icon import load_icon
from ..models.album_tree_model import AlbumTreeItem, AlbumTreeModel, AlbumTreeRole, NodeType
from ..palette import (
    SIDEBAR_BRANCH_CONTENT_GAP,
    SIDEBAR_DISABLED_TEXT_COLOR,
    SIDEBAR_HIGHLIGHT_MARGIN_X,
    SIDEBAR_HIGHLIGHT_MARGIN_Y,
    SIDEBAR_HOVER_BACKGROUND,
    SIDEBAR_ICON_COLOR,
    SIDEBAR_ICON_SIZE,
    SIDEBAR_ICON_TEXT_GAP,
    SIDEBAR_INDENT_PER_LEVEL,
    SIDEBAR_INDICATOR_SIZE,
    SIDEBAR_INDICATOR_SLOT_WIDTH,
    SIDEBAR_LEFT_PADDING,
    SIDEBAR_ROW_HEIGHT,
    SIDEBAR_ROW_RADIUS,
    SIDEBAR_SECTION_TEXT_COLOR,
    SIDEBAR_SELECTED_BACKGROUND,
    SIDEBAR_SEPARATOR_COLOR,
    SIDEBAR_TEXT_COLOR, SIDEBAR_ICON_COLOR_HEX,
)


@dataclass(slots=True)
class _IndicatorState:
    """Track the rendering state for a branch indicator."""

    angle: float = 0.0
    animation: QVariantAnimation | None = None


@dataclass(slots=True)
class _PaintState:
    """Hold derived data needed to render a single tree row."""

    index: QModelIndex
    rect: QRect
    node_type: NodeType
    tree_view: QTreeView | None
    is_enabled: bool
    is_selected: bool
    is_hover: bool
    indentation: int


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


# Shared stroke width override applied to sidebar icons to reduce visual aliasing.
SIDEBAR_ICON_STROKE_WIDTH = 2.0


class AlbumSidebarDelegate(QStyledItemDelegate):
    """Custom delegate painting the sidebar with a macOS inspired style."""

    def sizeHint(  # noqa: D401 - inherited docstring
        self, option: QStyleOptionViewItem, _index: QModelIndex
    ) -> QSize:
        width = option.rect.width()
        if width <= 0:
            width = 200
        return QSize(width, SIDEBAR_ROW_HEIGHT)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        painter.save()
        rect = option.rect
        node_type = index.data(AlbumTreeRole.NODE_TYPE) or NodeType.ALBUM

        if node_type == NodeType.SEPARATOR:
            self._draw_separator(painter, rect)
            painter.restore()
            return

        state = self._build_paint_state(option, index, rect, node_type)

        highlight = self._resolve_highlight_colour(state)
        if highlight is not None:
            self._draw_background(painter, rect, highlight)

        font = self._font_for_node(option.font, node_type)
        painter.setFont(font)

        x = rect.left() + SIDEBAR_LEFT_PADDING + state.indentation
        x = self._draw_branch_indicator(painter, state, x)
        icon = self._get_icon_for_paint_state(index, state)
        x = self._draw_icon(painter, option, icon, x)
        self._draw_text(painter, rect, font, index, state, x)

        painter.restore()

    def _get_icon_for_paint_state(self, index: QModelIndex, state: _PaintState) -> QIcon:
        """Return the correct icon for *index* based on the current paint *state*."""

        model = index.model()
        item = index.internalPointer()

        icon = QIcon()
        if isinstance(model, AlbumTreeModel) and isinstance(item, AlbumTreeItem):
            # Re-fetch the icon so we can enforce a consistent stroke width override.
            icon = model._icon_for_item(item, stroke_width=SIDEBAR_ICON_STROKE_WIDTH)
        else:
            # Fallback to whatever the model exposed if the expected types differ.
            data = index.data(Qt.ItemDataRole.DecorationRole)
            if isinstance(data, QIcon):
                icon = data

        if icon.isNull():
            return QIcon()

        if isinstance(model, AlbumTreeModel) and isinstance(item, AlbumTreeItem):
            if state.node_type == NodeType.STATIC:
                icon_base = model._STATIC_ICON_MAP.get(item.title.casefold())
                if icon_base in {"video", "suit.heart"}:
                    # The sidebar mirrors macOS behaviour where select states swap
                    # the regular outline icon for a filled version. We perform the
                    # decision here because the delegate has access to the selection
                    # state while the model intentionally does not.
                    suffix = ".fill" if state.is_selected else ""
                    return load_icon(
                        f"{icon_base}{suffix}.svg",
                        color=SIDEBAR_ICON_COLOR_HEX,
                        stroke_width=SIDEBAR_ICON_STROKE_WIDTH,
                    )

        return icon

    @staticmethod
    def _depth_for_index(index: QModelIndex) -> int:
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        return depth

    def _build_paint_state(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex,
        rect: QRect,
        node_type: NodeType,
    ) -> _PaintState:
        """Compute the immutable rendering state for the current index."""

        tree_view = self._resolve_tree_view(option)
        is_enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        depth = self._depth_for_index(index)
        indentation = depth * SIDEBAR_INDENT_PER_LEVEL

        return _PaintState(
            index=index,
            rect=rect,
            node_type=node_type,
            tree_view=tree_view,
            is_enabled=is_enabled,
            is_selected=is_selected,
            is_hover=is_hover,
            indentation=indentation,
        )

    def _resolve_tree_view(self, option: QStyleOptionViewItem) -> QTreeView | None:
        """Locate the owning :class:`QTreeView` if one is available."""

        if isinstance(option.widget, QTreeView):
            return option.widget
        parent = self.parent()
        if isinstance(parent, QTreeView):
            return parent
        return None

    def _resolve_highlight_colour(self, state: _PaintState) -> QColor | None:
        """Return the hover or selection colour, if the item supports it."""

        if state.node_type in {NodeType.SECTION, NodeType.SEPARATOR}:
            return None
        if state.is_selected:
            return SIDEBAR_SELECTED_BACKGROUND
        if state.is_hover and state.is_enabled:
            return SIDEBAR_HOVER_BACKGROUND
        return None

    def _draw_background(self, painter: QPainter, rect: QRect, colour: QColor) -> None:
        """Paint the rounded selection background using *colour*."""

        background_rect = rect.adjusted(
            SIDEBAR_HIGHLIGHT_MARGIN_X,
            SIDEBAR_HIGHLIGHT_MARGIN_Y,
            -SIDEBAR_HIGHLIGHT_MARGIN_X,
            -SIDEBAR_HIGHLIGHT_MARGIN_Y,
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawRoundedRect(background_rect, SIDEBAR_ROW_RADIUS, SIDEBAR_ROW_RADIUS)

    def _font_for_node(self, base_font: QFont, node_type: NodeType) -> QFont:
        """Return a correctly styled font for *node_type*."""

        font = QFont(base_font)
        if node_type == NodeType.HEADER:
            font.setPointSizeF(font.pointSizeF() + 1.0)
            font.setBold(True)
        elif node_type == NodeType.SECTION:
            font.setPointSizeF(font.pointSizeF() - 0.5)
            font.setCapitalization(QFont.Capitalization.SmallCaps)
        if node_type == NodeType.ACTION:
            font.setItalic(True)
        return font

    def _draw_branch_indicator(self, painter: QPainter, state: _PaintState, x: int) -> int:
        """Draw the disclosure triangle and return the next text origin."""

        tree_view = state.tree_view
        index = state.index
        model = index.model()
        has_children = bool(model is not None and model.hasChildren(index))

        if tree_view is None:
            return x + (SIDEBAR_INDICATOR_SLOT_WIDTH if state.indentation > 0 else 0)

        if not has_children:
            if state.indentation > 0:
                return x + SIDEBAR_INDICATOR_SLOT_WIDTH
            return x

        branch_rect = QRect(
            x,
            state.rect.top() + (state.rect.height() - SIDEBAR_INDICATOR_SIZE) // 2,
            SIDEBAR_INDICATOR_SIZE,
            SIDEBAR_INDICATOR_SIZE,
        )

        controller = getattr(tree_view, "branch_indicator_controller", None)
        angle = (
            controller.angle_for_index(index)
            if controller is not None
            else (90.0 if tree_view.isExpanded(index) else 0.0)
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        indicator_colour = SIDEBAR_TEXT_COLOR if state.is_enabled else SIDEBAR_DISABLED_TEXT_COLOR
        pen = QPen(indicator_colour)
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

        return branch_rect.right() + SIDEBAR_BRANCH_CONTENT_GAP

    def _draw_icon(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        icon: QIcon,
        x: int,
    ) -> int:
        """Render *icon* and return the x coordinate following the glyph."""

        if icon.isNull():
            return x

        icon_rect = QRect(
            x,
            option.rect.top() + (option.rect.height() - SIDEBAR_ICON_SIZE) // 2,
            SIDEBAR_ICON_SIZE,
            SIDEBAR_ICON_SIZE,
        )
        icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)
        return icon_rect.right() + SIDEBAR_ICON_TEXT_GAP

    def _draw_text(
        self,
        painter: QPainter,
        rect: QRect,
        font: QFont,
        index: QModelIndex,
        state: _PaintState,
        x: int,
    ) -> None:
        """Draw the item text with elision if required."""

        text_colour = self._text_colour_for_state(state)
        painter.setPen(text_colour)

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        metrics = QFontMetrics(font)
        text_rect = rect.adjusted(x - rect.left(), 0, -8, 0)
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

    def _text_colour_for_state(self, state: _PaintState) -> QColor:
        """Return the correct foreground colour for the row."""

        if state.node_type == NodeType.SECTION:
            return SIDEBAR_SECTION_TEXT_COLOR
        if state.node_type == NodeType.ACTION:
            return SIDEBAR_ICON_COLOR
        if not state.is_enabled:
            return SIDEBAR_DISABLED_TEXT_COLOR
        return SIDEBAR_TEXT_COLOR

    def _draw_separator(self, painter: QPainter, rect: QRect) -> None:
        """Render a horizontal rule used to group tree sections."""

        pen = QPen(SIDEBAR_SEPARATOR_COLOR)
        pen.setWidth(1)
        painter.setPen(pen)
        y = rect.center().y()
        painter.drawLine(
            rect.left() + SIDEBAR_LEFT_PADDING,
            y,
            rect.right() - SIDEBAR_LEFT_PADDING,
            y,
        )


__all__ = [
    "AlbumSidebarDelegate",
    "BranchIndicatorController",
]
