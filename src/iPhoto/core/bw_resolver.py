"""Helpers translating Black & White adjustments between UI and renderers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict

import numpy as np

if TYPE_CHECKING:
    from PySide6.QtGui import QImage
    from .image_filters import apply_adjustments


@dataclass(frozen=True)
class BWParams:
    """Container bundling the Black & White adjustment parameters."""

    intensity: float = 0.5
    neutrals: float = 0.0
    tone: float = 0.0
    grain: float = 0.0
    master: float = 0.5

    def clamp(self) -> "BWParams":
        """Return a clamped copy that respects the slider ranges.

        The edit UI keeps values within the supported ranges, but thumbnail
        generators and deserialisation helpers call this method as an extra
        safety net so downstream renderers never receive out-of-range uniforms.
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


def _clamp01(value: float) -> float:
    """Return ``value`` constrained to the ``[0.0, 1.0]`` range."""

    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def gauss(mu: float, sigma: float, value: float) -> float:
    """Return the normal distribution weight for *value* given *mu*/*sigma*."""

    if sigma <= 0.0:
        return 0.0
    distance = (value - mu) / sigma
    return float(math.exp(-0.5 * distance * distance))


def mix3(left: float, center: float, right: float, w_left: float, w_center: float, w_right: float) -> float:
    """Return a weighted blend of the three anchor values."""

    weights = np.array([w_left, w_center, w_right], dtype=float)
    total = float(np.sum(weights))
    if total <= 1e-8:
        return center
    normalised = weights / total
    anchors = np.array([left, center, right], dtype=float)
    return float(np.sum(anchors * normalised))


# Anchor definitions mirror the GLSL shader so CPU previews and GPU renders match.
ANCHOR_CENTER: Dict[str, float] = {
    "Intensity": 0.50,
    "Neutrals": 0.00,
    "Tone": 0.00,
}
ANCHOR_LEFT: Dict[str, float] = {
    "Intensity": 0.20,
    "Neutrals": 0.20,
    "Tone": 0.10,
}
ANCHOR_RIGHT: Dict[str, float] = {
    "Intensity": 0.80,
    "Neutrals": 0.10,
    "Tone": 0.60,
}

# Defaults are expressed using shader terminology so documentation remains consistent.
DEFAULTS = {"Intensity": 0.50, "Neutrals": 0.00, "Tone": 0.00}


def resolve_effective_params(master: float, user_vals: BWParams) -> BWParams:
    """Return the effective shader parameters for *master* and *user_vals*.

    The computation mirrors :mod:`BW_final.py` so the CPU thumbnails, GPU
    renderer, and persisted session values stay perfectly aligned.  The master
    slider selects a weighted combination of three anchor looks via Gaussian
    kernels and the user sliders provide offsets relative to the neutral anchor.
    """

    master_clamped = _clamp(master, 0.0, 1.0)

    sig_l = 0.30
    sig_c = 0.26
    sig_r = 0.30
    weight_left = gauss(0.00, sig_l, master_clamped)
    weight_center = gauss(0.50, sig_c, master_clamped)
    weight_right = gauss(1.00, sig_r, master_clamped)

    anchors: Dict[str, float] = {}
    for key in ("Intensity", "Neutrals", "Tone"):
        anchors[key] = mix3(
            ANCHOR_LEFT[key],
            ANCHOR_CENTER[key],
            ANCHOR_RIGHT[key],
            weight_left,
            weight_center,
            weight_right,
        )

    effective_intensity = anchors["Intensity"] + (user_vals.intensity - DEFAULTS["Intensity"])
    effective_neutrals = anchors["Neutrals"] + (user_vals.neutrals - DEFAULTS["Neutrals"])
    effective_tone = anchors["Tone"] + (user_vals.tone - DEFAULTS["Tone"])

    return BWParams(
        intensity=_clamp(effective_intensity, 0.0, 1.0),
        neutrals=_clamp(effective_neutrals, 0.0, 1.0),
        tone=_clamp(effective_tone, 0.0, 1.0),
        grain=_clamp(user_vals.grain, 0.0, 1.0),
        master=master_clamped,
    )


def apply_bw_preview(image: "QImage", effective_params: BWParams, *, enabled: bool = True) -> "QImage":
    """Return a preview frame with *effective_params* applied on top of *image*."""

    from .image_filters import apply_adjustments

    clamped = effective_params.clamp()
    adjustments = {
        "BW_Enabled": bool(enabled),
        "BW_Intensity": clamped.intensity,
        "BW_Neutrals": clamped.neutrals,
        "BW_Tone": clamped.tone,
        "BW_Grain": clamped.grain,
    }
    return apply_adjustments(image, adjustments)


__all__ = [
    "ANCHOR_CENTER",
    "ANCHOR_LEFT",
    "ANCHOR_RIGHT",
    "BWParams",
    "DEFAULTS",
    "apply_bw_preview",
    "resolve_effective_params",
]
