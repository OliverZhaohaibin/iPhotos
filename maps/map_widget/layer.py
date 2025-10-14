"""Definitions for describing how vector tile layers are rendered."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LayerPlan:
    """Describe how a vector tile layer should be rendered."""

    source_layer: str
    style_layer: str
    kind: str  # "fill", "line", or "symbol"
    is_lonlat: bool = False


__all__ = ["LayerPlan"]
