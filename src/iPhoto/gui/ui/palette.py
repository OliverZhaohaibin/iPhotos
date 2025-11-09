"""Shared colour utilities and constants for the Qt GUI layer."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette, QFont
from PySide6.QtWidgets import QWidget

# --- Sidebar colour palette -------------------------------------------------
# The macOS-inspired blue used for key sidebar affordances and icon tinting.
SIDEBAR_ICON_COLOR_HEX = "#1e73ff"
SIDEBAR_ICON_COLOR = QColor(SIDEBAR_ICON_COLOR_HEX)

# Core structural colours that govern the sidebar look and feel.
SIDEBAR_BACKGROUND_COLOR = QColor("#eef3f6")
SIDEBAR_TEXT_COLOR = QColor("#2b2b2b")
SIDEBAR_TITLE_COLOR_HEX = "#1b1b1b"
SIDEBAR_SECTION_TEXT_COLOR = QColor(0, 0, 0, 160)
SIDEBAR_DISABLED_TEXT_COLOR = QColor(0, 0, 0, 90)
SIDEBAR_SEPARATOR_COLOR = QColor(0, 0, 0, 40)

# Interaction feedback colours for hover, selection, and focus states.
SIDEBAR_HOVER_BACKGROUND = QColor(0, 0, 0, 24)
SIDEBAR_SELECTED_BACKGROUND = QColor(0, 0, 0, 56)

# --- Sidebar metrics --------------------------------------------------------
# These values ensure the delegate, tree view, and hit testing all share the
# same geometry assumptions.
SIDEBAR_ROW_HEIGHT = 36
SIDEBAR_ROW_RADIUS = 10
SIDEBAR_LEFT_PADDING = 14
SIDEBAR_ICON_TEXT_GAP = 10
SIDEBAR_BRANCH_CONTENT_GAP = 6
SIDEBAR_INDICATOR_HOTZONE_MARGIN = 4
SIDEBAR_INDENT_PER_LEVEL = 22
SIDEBAR_INDICATOR_SLOT_WIDTH = 22
SIDEBAR_INDICATOR_SIZE = 16
SIDEBAR_ICON_SIZE = 24

# Margins around the rounded selection pill.
SIDEBAR_HIGHLIGHT_MARGIN_X = 6
SIDEBAR_HIGHLIGHT_MARGIN_Y = 4

# Tree level styling shared by the widget and delegate.
SIDEBAR_TREE_MIN_WIDTH = 220
SIDEBAR_TREE_STYLESHEET = (
    "QTreeView { background: transparent; border: none; }"
    "QTreeView::item { border: 0px; padding: 0px; margin: 0px; }"
    "QTreeView::branch { image: none; }"
)

# Default layout chrome values for the sidebar wrapper widget.
SIDEBAR_LAYOUT_MARGIN = (12, 12, 12, 12)
SIDEBAR_LAYOUT_SPACING = 8

# ---Edit Sidebar metrics --------------------------------------------------------
Edit_SIDEBAR_FONT = QFont()
Edit_SIDEBAR_FONT.setPointSize(10)
Edit_SIDEBAR_FONT.setWeight(QFont.Weight.Bold)

Edit_SIDEBAR_SUB_FONT = QFont()
Edit_SIDEBAR_SUB_FONT.setPointSize(8)
Edit_SIDEBAR_SUB_FONT.setWeight(QFont.Weight.Medium)

def viewer_surface_color(widget: QWidget) -> str:
    """Return the name of the palette-derived viewer surface colour.

    Using the palette keeps every media canvas perfectly aligned with the
    surrounding chrome, eliminating the subtle mismatches that appear when a
    hard-coded hex value is used instead.  ``widget`` is any control that lives
    inside the detail panel; its palette already reflects the final window
    styling so deriving the colour from it guarantees an exact match.
    """

    background_role = widget.backgroundRole()
    if background_role == QPalette.ColorRole.NoRole:
        background_role = QPalette.ColorRole.Window
    return widget.palette().color(background_role).name()


__all__ = [
    "SIDEBAR_ICON_COLOR_HEX",
    "SIDEBAR_ICON_COLOR",
    "SIDEBAR_BACKGROUND_COLOR",
    "SIDEBAR_TEXT_COLOR",
    "SIDEBAR_TITLE_COLOR_HEX",
    "SIDEBAR_SECTION_TEXT_COLOR",
    "SIDEBAR_DISABLED_TEXT_COLOR",
    "SIDEBAR_SEPARATOR_COLOR",
    "SIDEBAR_HOVER_BACKGROUND",
    "SIDEBAR_SELECTED_BACKGROUND",
    "SIDEBAR_ROW_HEIGHT",
    "SIDEBAR_ROW_RADIUS",
    "SIDEBAR_LEFT_PADDING",
    "SIDEBAR_ICON_TEXT_GAP",
    "SIDEBAR_BRANCH_CONTENT_GAP",
    "SIDEBAR_INDICATOR_HOTZONE_MARGIN",
    "SIDEBAR_INDENT_PER_LEVEL",
    "SIDEBAR_INDICATOR_SLOT_WIDTH",
    "SIDEBAR_INDICATOR_SIZE",
    "SIDEBAR_ICON_SIZE",
    "SIDEBAR_HIGHLIGHT_MARGIN_X",
    "SIDEBAR_HIGHLIGHT_MARGIN_Y",
    "SIDEBAR_TREE_MIN_WIDTH",
    "SIDEBAR_TREE_STYLESHEET",
    "SIDEBAR_LAYOUT_MARGIN",
    "SIDEBAR_LAYOUT_SPACING",
    "viewer_surface_color",
]
