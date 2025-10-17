"""Shared colour constants for the Qt GUI layer."""

from __future__ import annotations

# The macOS-inspired blue used for key sidebar affordances and icon tinting.
SIDEBAR_ICON_COLOR_HEX = "#1e73ff"

# Soft neutral tone that mirrors the overall window chrome used for media viewing
# surfaces.  The slight warmth keeps photos and videos from floating on stark
# white while still providing a bright, distraction-free backdrop.
VIEWER_SURFACE_COLOR_HEX = "#f2f2f7"

__all__ = ["SIDEBAR_ICON_COLOR_HEX", "VIEWER_SURFACE_COLOR_HEX"]
