"""Shared constants for the custom frameless window chrome.

The metrics are required both by :mod:`ui_main_window` (for the primary window
title bar) and by smaller floating widgets such as
``iPhoto.gui.ui.widgets.info_panel``.  Centralising the constants in this
module prevents circular imports between ``ui_main_window`` and the widget
package while keeping the values authoritative in a single location.
"""

from __future__ import annotations

from PySide6.QtCore import QSize

# The glyph size determines the logical resolution used when rendering SVG
# assets for the traffic-light style window buttons.  Matching the macOS
# appearance requires 16×16 device-independent pixels; using ``QSize`` keeps the
# API in line with Qt's expectations for icon geometry.
WINDOW_CONTROL_GLYPH_SIZE = QSize(16, 16)

# The clickable area for the title bar controls.  Maintaining a consistent
# 26×26 pixel hit target ensures the controls remain easy to activate regardless
# of platform DPI scaling while perfectly aligning with the rounded window
# chrome.
WINDOW_CONTROL_BUTTON_SIZE = QSize(26, 26)


__all__ = ["WINDOW_CONTROL_BUTTON_SIZE", "WINDOW_CONTROL_GLYPH_SIZE"]

