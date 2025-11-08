"""Helpers translating Black & White adjustments between UI and renderers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict

from PySide6.QtGui import QImage

from .image_filters import apply_adjustments


@dataclass(frozen=True)
class BWParams:
    """Container bundling the Black & White adjustment parameters in ``[0, 1]``."""

    intensity: float = 0.5
    neutrals: float = 0.0
    tone: float = 0.0
    grain: float = 0.0
    master: float = 0.5

    def clamp(self) -> "BWParams":
        """Return a clamped copy that respects the slider ranges.

        The edit UI should already keep values within the supported ranges, but
        thumbnail generators and deserialisation helpers call this method as an
        additional safety net so downstream renderers never receive out-of-range
        uniforms.
        """

        return BWParams(
            intensity=_clamp(self.intensity, 0.0, 1.0),
            neutrals=_clamp(self.neutrals, 0.0, 1.0),
            tone=_clamp(self.tone, 0.0, 1.0),
            grain=_clamp(self.grain, 0.0, 1.0),
            master=_clamp(self.master, 0.0, 1.0),
        )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """Return *value* constrained to ``[minimum, maximum]``."""

    return max(minimum, min(maximum, float(value)))


# Anchor definitions mirror the GLSL shader so CPU previews and GPU renders match.
_ANCHOR_CENTER: Dict[str, float] = {
    "Intensity": 0.50,
    "Neutrals": 0.00,
    "Tone": 0.00,
}
_ANCHOR_LEFT: Dict[str, float] = {
    "Intensity": 0.20,
    "Neutrals": 0.20,
    "Tone": 0.10,
}
_ANCHOR_RIGHT: Dict[str, float] = {
    "Intensity": 0.80,
    "Neutrals": 0.10,
    "Tone": 0.60,
}


def aggregate_curve(master: float) -> Dict[str, float]:
    """Return derived parameters for the master slider position *master*.

    The anchor interpolation mirrors :mod:`BW_final.py` so that the master slider
    transitions smoothly between "soft", "neutral", and "rich" looks while
    generating values in the ``[0, 1]`` range consumed by both the OpenGL shader
    and the CPU thumbnail renderer.
    """

    master = _clamp(master, 0.0, 1.0)

    def gauss(mu: float, sigma: float, value: float) -> float:
        """Return the Gaussian weight centred on *mu* evaluated at *value*."""

        if sigma <= 0.0:
            return 0.0
        delta = (value - mu) / sigma
        return math.exp(-0.5 * delta * delta)

    def mix3(left: float, centre: float, right: float, w_l: float, w_c: float, w_r: float) -> float:
        """Return the weighted blend of three anchor values.

        The helper normalises the weights so callers can supply raw Gaussian
        amplitudes without worrying about drift when the master slider hugs the
        edges of the track.
        """

        weight_sum = w_l + w_c + w_r
        if weight_sum <= 1e-8:
            return centre
        inv = 1.0 / weight_sum
        return left * w_l * inv + centre * w_c * inv + right * w_r * inv

    sigma_left = 0.30
    sigma_centre = 0.26
    sigma_right = 0.30

    w_left = gauss(0.0, sigma_left, master)
    w_centre = gauss(0.5, sigma_centre, master)
    w_right = gauss(1.0, sigma_right, master)

    return {
        key: _clamp(
            mix3(
                _ANCHOR_LEFT[key],
                _ANCHOR_CENTER[key],
                _ANCHOR_RIGHT[key],
                w_left,
                w_centre,
                w_right,
            ),
            0.0,
            1.0,
        )
        for key in _ANCHOR_CENTER
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
    new B&W implementation mirrors the OpenGL shader and therefore expects
    ``[0.0, 1.0]`` inputs for intensity, neutrals, tone and grain.  The grain
    effect is intentionally included because the edit preview already renders
    per-frame noise and we want thumbnails to match the live viewer.
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
