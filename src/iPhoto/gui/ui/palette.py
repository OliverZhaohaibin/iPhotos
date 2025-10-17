"""Centralised colour palette utilities for the Qt GUI layer."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

# ---------------------------------------------------------------------------
# Sidebar palette
# ---------------------------------------------------------------------------
# The sidebar mimics the macOS Photos chrome with a soft blue-grey background
# and blue accent icons for primary navigation entries. Collecting the colour
# constants here keeps the delegate and container widgets free from duplicated
# literals, making future theme adjustments a single-file change.
SIDEBAR_BACKGROUND = QColor("#eef3f6")
SIDEBAR_TEXT = QColor("#2b2b2b")
SIDEBAR_ICON_ACCENT = QColor("#1e73ff")
SIDEBAR_HOVER_BACKGROUND = QColor(0, 0, 0, 24)
SIDEBAR_SELECTION_BACKGROUND = QColor(0, 0, 0, 56)
SIDEBAR_DISABLED_TEXT = QColor(0, 0, 0, 90)
SIDEBAR_SECTION_TEXT = QColor(0, 0, 0, 160)
SIDEBAR_SEPARATOR = QColor(0, 0, 0, 40)


def viewer_surface_color(widget: QWidget) -> str:
    """Return the palette-derived surface colour for media viewers.

    Media canvases (image viewer, video viewport, and placeholder panes) should
    blend seamlessly into the surrounding interface. Querying the background
    colour directly from the widget palette avoids subtle mismatches caused by
    hard-coded hex values and keeps custom Qt stylesheets in sync.
    """

    background_role = widget.backgroundRole()
    if background_role == QPalette.ColorRole.NoRole:
        background_role = QPalette.ColorRole.Window
    return widget.palette().color(background_role).name()


__all__ = [
    "SIDEBAR_BACKGROUND",
    "SIDEBAR_TEXT",
    "SIDEBAR_ICON_ACCENT",
    "SIDEBAR_HOVER_BACKGROUND",
    "SIDEBAR_SELECTION_BACKGROUND",
    "SIDEBAR_DISABLED_TEXT",
    "SIDEBAR_SECTION_TEXT",
    "SIDEBAR_SEPARATOR",
    "viewer_surface_color",
]
