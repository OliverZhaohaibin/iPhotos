"""Blend the Light master slider with optional fine-tuning overrides."""

from __future__ import annotations

from typing import Mapping, MutableMapping
import math

# The order matches the non-destructive editing pipeline so the same tuple can be reused when
# iterating over the stored adjustments, writing sidecar files, or rendering previews.  Keeping the
# keys centralised avoids subtle mismatches across the UI and IO layers.
LIGHT_KEYS = (
    "Brilliance",
    "Exposure",
    "Highlights",
    "Shadows",
    "Brightness",
    "Contrast",
    "BlackPoint",
)


def _clamp(value: float, minimum: float = -1.0, maximum: float = 1.0) -> float:
    """Return *value* limited to the inclusive ``[minimum, maximum]`` range."""

    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _soften_master_value(value: float, response: float = 1.6) -> float:
    """Return a softened version of *value* using a perceptual S-curve."""

    # ``math.tanh`` provides a smooth transition that feels closer to how the Photos slider behaves
    # when approaching the extremes.  The ``response`` factor controls how quickly the curve eases
    # in and out of the bounds while the denominator normalises the output back into ``[-1, 1]``.
    return math.tanh(response * value) / math.tanh(response)


def resolve_light_vector(
    master: float,
    overrides: Mapping[str, float] | None = None,
    *,
    mode: str = "delta",
) -> dict[str, float]:
    """Return the seven Light adjustments derived from *master* and *overrides*.

    Parameters
    ----------
    master:
        Value of the Light master slider in the ``[-1.0, 1.0]`` range.
    overrides:
        Optional mapping providing user supplied tweaks for the fine-tuning controls.
    mode:
        ``"delta"`` (default) treats overrides as additive deltas, ``"absolute"`` replaces the
        computed values entirely.  Any other value raises :class:`ValueError`.
    """

    master_clamped = _clamp(master)
    master_soft = _soften_master_value(master_clamped)

    if master_soft >= 0.0:
        base = {
            "Exposure": 0.55 * master_soft,
            "Brightness": 0.35 * master_soft,
            "Brilliance": 0.45 * master_soft,
            "Shadows": 0.60 * master_soft,
            "Highlights": -0.25 * master_soft,
            "Contrast": -0.10 * master_soft,
            "BlackPoint": -0.10 * master_soft,
        }
    else:
        base = {
            "Exposure": 0.50 * master_soft,
            "Brightness": 0.40 * master_soft,
            "Brilliance": 0.30 * master_soft,
            "Shadows": 0.50 * master_soft,
            "Highlights": 0.20 * master_soft,
            "Contrast": -0.15 * master_soft,
            "BlackPoint": 0.25 * (-master_soft),
        }

    for key, value in list(base.items()):
        base[key] = _clamp(value)

    overrides = overrides or {}
    resolved: MutableMapping[str, float] = dict(base)
    if mode == "delta":
        for key, value in overrides.items():
            if key in LIGHT_KEYS:
                resolved[key] = _clamp(resolved.get(key, 0.0) + float(value))
    elif mode == "absolute":
        for key, value in overrides.items():
            if key in LIGHT_KEYS:
                resolved[key] = _clamp(float(value))
    else:
        raise ValueError("mode must be 'delta' or 'absolute'")

    for key in LIGHT_KEYS:
        resolved.setdefault(key, 0.0)

    return dict(resolved)


def build_light_adjustments(
    master: float,
    options: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Convenience helper returning Light adjustments using delta override semantics."""

    return resolve_light_vector(master, options, mode="delta")
