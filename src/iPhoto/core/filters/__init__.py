"""Modular image filtering package for non-destructive photo editing.

This package provides image adjustment functionality through a clean separation
of concerns:
- algorithms: Pure mathematical functions for image processing
- executors: Different implementation strategies (JIT, Pillow, NumPy, fallback)
- utils: Platform-specific utilities
"""

from __future__ import annotations

# Re-export the main API for backwards compatibility
from .facade import apply_adjustments

__all__ = ["apply_adjustments"]
