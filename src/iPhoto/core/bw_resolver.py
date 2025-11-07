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
            intensity=_clamp(self.intensity, 0.0, 1.0),
            neutrals=_clamp(self.neutrals, -1.0, 1.0),
            tone=_clamp(self.tone, -1.0, 1.0),
            grain=_clamp(self.grain, 0.0, 1.0),
            master=_clamp(self.master, 0.0, 1.0),
        )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """Return *value* constrained to ``[minimum, maximum]``."""

    return max(minimum, min(maximum, float(value)))


def smooth01(value: float) -> float:
    """Smoothly interpolate ``value`` within ``[0, 1]`` using a Hermite curve."""

    clamped = _clamp(value, 0.0, 1.0)
    return clamped * clamped * (3.0 - 2.0 * clamped)


def aggregate_curve(master: float) -> Dict[str, float]:
    """Return derived parameters for the master slider position *master*.

    The master slider simultaneously tweaks intensity, neutrals, and tone using
    a Photos-inspired curve.  Returning a mapping keeps the helper flexible so
    both the UI and CPU preview pipeline can consume the same transformation
    without duplicating the maths.
    """

    master = _clamp(master, 0.0, 1.0)
    intensity = smooth01(master)
    neutrals = _clamp(0.25 * (2.0 * master - 1.0), -1.0, 1.0)
    tone = _clamp(-0.10 + 0.60 * master, -1.0, 1.0)
    return {
        "Intensity": intensity,
        "Neutrals": neutrals,
        "Tone": tone,
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
    "smooth01",
]
