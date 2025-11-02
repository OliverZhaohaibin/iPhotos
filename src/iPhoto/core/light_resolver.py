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
        Value of the Light master slider in the ``[-1.0, 1.0]`` range.  The response curve and the
        ``master_strength`` factor keep the resulting adjustments within a subtle ±0.1 window.
    overrides:
        Optional mapping providing user supplied tweaks for the fine-tuning controls.  Deltas are
        scaled by ``delta_strength`` so the fine sliders share the same effective sensitivity as
        the master slider.
    mode:
        ``"delta"`` (default) treats overrides as additive deltas, ``"absolute"`` replaces the
        computed values entirely.  Any other value raises :class:`ValueError`.
    """

    # Strength factors keep the Light stack subtle so the master slider and the fine-tuning
    # deltas feel equally precise.  The chosen value mirrors the UX feedback that motivated this
    # change: a movement of one unit should result in roughly ten percent of the previous impact.
    master_strength = 0.1
    delta_strength = 0.1

    master_clamped = _clamp(master)
    master_soft = _soften_master_value(master_clamped)

    if master_soft >= 0.0:
        base = {
            # Each coefficient describes how strongly the master slider influences the individual
            # channel.  Multiplying the softened master value by ``master_strength`` keeps the
            # resulting value inside a subtle operating range.
            "Exposure": master_strength * 0.55 * master_soft,
            "Brightness": master_strength * 0.35 * master_soft,
            "Brilliance": master_strength * 0.45 * master_soft,
            "Shadows": master_strength * 0.60 * master_soft,
            "Highlights": master_strength * -0.25 * master_soft,
            "Contrast": master_strength * -0.10 * master_soft,
            "BlackPoint": master_strength * -0.10 * master_soft,
        }
    else:
        base = {
            "Exposure": master_strength * 0.50 * master_soft,
            "Brightness": master_strength * 0.40 * master_soft,
            "Brilliance": master_strength * 0.30 * master_soft,
            "Shadows": master_strength * 0.50 * master_soft,
            "Highlights": master_strength * 0.20 * master_soft,
            "Contrast": master_strength * -0.15 * master_soft,
            "BlackPoint": master_strength * 0.25 * (-master_soft),
        }

    for key, value in list(base.items()):
        base[key] = _clamp(value)

    overrides = overrides or {}
    resolved: MutableMapping[str, float] = dict(base)
    if mode == "delta":
        for key, value in overrides.items():
            if key in LIGHT_KEYS:
                # Session values represent deltas relative to the resolved base.  Scaling them by
                # ``delta_strength`` keeps user fine-tuning consistent with the new master range.
                resolved[key] = _clamp(
                    resolved.get(key, 0.0) + float(value) * delta_strength
                )
    elif mode == "absolute":
        for key, value in overrides.items():
            if key in LIGHT_KEYS:
                # ``absolute`` mode receives pre-normalised values, but we still apply the delta
                # strength so the caller does not need to duplicate the scaling rules here.
                resolved[key] = _clamp(float(value) * delta_strength)
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
