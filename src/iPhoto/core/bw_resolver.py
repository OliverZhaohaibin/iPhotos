"""Helpers translating Black & White adjustments between UI and renderers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from PySide6.QtGui import QImage

from .image_filters import apply_adjustments


@dataclass(frozen=True)
class BWParams:
    """Container bundling the Black & White adjustment parameters."""

    intensity: float = 0.0
    neutrals: float = 0.0
    tone: float = 0.0
    grain: float = 0.0
    master: float = 0.0

    def clamp(self) -> "BWParams":
        """Return a clamped copy that respects the slider ranges.

        The edit UI should already keep values within the supported ranges, but
        thumbnail generators and deserialisation helpers call this method as an
        additional safety net so downstream renderers never receive out-of-range
        uniforms.
        """

        return BWParams(
            intensity=_clamp(self.intensity, -1.0, 1.0),
            neutrals=_clamp(self.neutrals, -1.0, 1.0),
            tone=_clamp(self.tone, -1.0, 1.0),
            grain=_clamp(self.grain, 0.0, 1.0),
            master=_clamp(self.master, 0.0, 1.0),
        )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """Return *value* constrained to ``[minimum, maximum]``."""

    return max(minimum, min(maximum, float(value)))


def _clamp01(value: float) -> float:
    """Return ``value`` constrained to the ``[0.0, 1.0]`` range."""

    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _lerp(start: float, end: float, fraction: float) -> float:
    """Linearly interpolate between *start* and *end* using *fraction*."""

    return start + (end - start) * fraction


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    """Return a smooth interpolation factor between *edge0* and *edge1*."""

    if edge0 == edge1:
        return 0.0
    t = _clamp01((value - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


# Anchor definitions mirror the GLSL shader so CPU previews and GPU renders match.
_ANCHOR_CENTER: Dict[str, float] = {
    "Intensity": 0.0,
    "Neutrals": 0.0,
    "Tone": 0.0,
}
_ANCHOR_LEFT: Dict[str, float] = {
    "Intensity": -0.7,
    "Neutrals": 0.2,
    "Tone": -0.1,
}
_ANCHOR_RIGHT: Dict[str, float] = {
    "Intensity": 0.8,
    "Neutrals": -0.05,
    "Tone": 0.6,
}


def aggregate_curve(master: float) -> Dict[str, float]:
    """Return derived parameters for the master slider position *master*.

    The anchor interpolation mirrors :mod:`BW_final.py` so that the master slider
    transitions smoothly between "soft", "neutral", and "rich" looks while
    generating values in the ``[-1, 1]`` range expected by the updated shader.
    """

    master = _clamp(master, 0.0, 1.0)

    anchors: Dict[str, float]
    m = _clamp01(master)
    if m <= 0.5:
        s = _smoothstep(0.0, 0.5, m)
        anchors = {
            key: _lerp(_ANCHOR_LEFT[key], _ANCHOR_CENTER[key], s)
            for key in _ANCHOR_CENTER
        }
    else:
        s = _smoothstep(0.5, 1.0, m)
        anchors = {
            key: _lerp(_ANCHOR_CENTER[key], _ANCHOR_RIGHT[key], s)
            for key in _ANCHOR_CENTER
        }

    return {
        "Intensity": _clamp(anchors["Intensity"], -1.0, 1.0),
        "Neutrals": _clamp(anchors["Neutrals"], -1.0, 1.0),
        "Tone": _clamp(anchors["Tone"], -1.0, 1.0),
    }


def params_from_master(master: float, *, grain: float = 0.0) -> BWParams:
    """Return a :class:`BWParams` instance resolved from *master* and *grain*."""

    curve = aggregate_curve(master)
    return BWParams(
        intensity=curve["Intensity"],
        neutrals=curve["Neutrals"],
        tone=curve["Tone"],
        grain=_clamp(grain, 0.0, 1.0),
        master=_clamp(master, 0.0, 1.0),
    )


def apply_bw_preview(image: QImage, params: BWParams, *, enabled: bool = True) -> QImage:
    """Return a preview frame with *params* applied on top of *image*.

    The helper feeds :func:`apply_adjustments` with a minimal adjustment mapping
    so the CPU thumbnail pipeline reuses the production-tested tone curves.  The
    grain effect is intentionally included because the edit preview already
    renders per-frame noise and we want thumbnails to match the live viewer.
    """

    clamped = params.clamp()
    adjustments = {
        "BW_Enabled": bool(enabled),
        "BW_Intensity": clamped.intensity,
        "BW_Neutrals": clamped.neutrals,
        "BW_Tone": clamped.tone,
        "BW_Grain": clamped.grain,
    }
    return apply_adjustments(image, adjustments)


__all__ = [
    "BWParams",
    "aggregate_curve",
    "apply_bw_preview",
    "params_from_master",
]
