"""Public package interface for the map widget components.

This module re-exports the high-level classes that external callers relied on
before the refactor, keeping backwards compatibility for imports such as
``from map_widget import LayerPlan``.
"""

from .layer import LayerPlan
from .map_gl_widget import MapGLWidget
from .map_widget import MapWidget

__all__ = ["MapWidget", "MapGLWidget", "LayerPlan"]
