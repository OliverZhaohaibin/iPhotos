"""Shared geometry and colour constants used by the main window widgets."""

from PySide6.QtCore import QSize

# Header ---------------------------------------------------------------------

HEADER_ICON_GLYPH_SIZE = QSize(24, 24)
"""Standard glyph size (in device-independent pixels) for header icons."""

HEADER_BUTTON_SIZE = QSize(36, 38)
"""Hit target size that guarantees a comfortable clickable header button."""

EDIT_HEADER_BUTTON_HEIGHT = HEADER_BUTTON_SIZE.height()
"""Uniform vertical extent for every interactive control in the edit toolbar."""

# Edit toolbar ---------------------------------------------------------------

EDIT_DONE_BUTTON_BACKGROUND = "#FFD60A"
"""Primary accent colour that mirrors the Photos.app done button."""

EDIT_DONE_BUTTON_BACKGROUND_HOVER = "#FFE066"
"""Softer hover tint that preserves contrast against the yellow accent."""

EDIT_DONE_BUTTON_BACKGROUND_PRESSED = "#FFC300"
"""Darker pressed-state shade to communicate the button click."""

EDIT_DONE_BUTTON_BACKGROUND_DISABLED = "#CFC2A0"
"""Muted disabled colour that still reads as part of the yellow palette."""

EDIT_DONE_BUTTON_TEXT_COLOR = "#1C1C1E"
"""High-contrast foreground colour suitable for the yellow accent."""

EDIT_DONE_BUTTON_TEXT_DISABLED = "#7F7F7F"
"""Subdued text colour that keeps disabled labels legible."""

# Window chrome --------------------------------------------------------------

WINDOW_CONTROL_GLYPH_SIZE = QSize(16, 16)
"""Icon size used for the custom window chrome buttons."""

WINDOW_CONTROL_BUTTON_SIZE = QSize(26, 26)
"""Provides a reliable click target for the frameless window controls."""

TITLE_BAR_HEIGHT = WINDOW_CONTROL_BUTTON_SIZE.height() + 16
"""Fixed vertical extent for the custom title bar including padding."""

__all__ = [
    "HEADER_ICON_GLYPH_SIZE",
    "HEADER_BUTTON_SIZE",
    "EDIT_HEADER_BUTTON_HEIGHT",
    "EDIT_DONE_BUTTON_BACKGROUND",
    "EDIT_DONE_BUTTON_BACKGROUND_HOVER",
    "EDIT_DONE_BUTTON_BACKGROUND_PRESSED",
    "EDIT_DONE_BUTTON_BACKGROUND_DISABLED",
    "EDIT_DONE_BUTTON_TEXT_COLOR",
    "EDIT_DONE_BUTTON_TEXT_DISABLED",
    "WINDOW_CONTROL_GLYPH_SIZE",
    "WINDOW_CONTROL_BUTTON_SIZE",
    "TITLE_BAR_HEIGHT",
]
