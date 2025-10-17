"""Shared colour utilities for the Qt GUI layer."""

from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QWidget

# The macOS-inspired blue used for key sidebar affordances and icon tinting.
SIDEBAR_ICON_COLOR_HEX = "#1e73ff"


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


__all__ = ["SIDEBAR_ICON_COLOR_HEX", "viewer_surface_color"]
