"""Pure image processing algorithms independent of platform and data format.

This module contains the core mathematical logic for image adjustments,
implemented using Numba JIT compilation for performance. These functions
operate on normalized float values (0.0 - 1.0) and basic types, with no
dependencies on Qt, Pillow, or specific image formats.
"""

from __future__ import annotations

import math

from numba import jit


@jit(nopython=True, inline="always")
def _apply_channel_adjustments(
    value: float,
    exposure: float,
    brightness: float,
    brilliance: float,
    highlights: float,
    shadows: float,
    contrast_factor: float,
    black_point: float,
) -> float:
    """Apply the tone curve adjustments to a single normalised channel."""

    # Exposure/brightness work in log space in photo editors.  The simplified
    # version below keeps the UI intuitive without introducing heavy maths.
    adjusted = value + exposure + brightness

    # Brilliance nudges mid-tones while preserving highlights and deep shadows.
    mid_distance = value - 0.5
    adjusted += brilliance * (1.0 - (mid_distance * 2.0) ** 2)

    # Highlights emphasise values near the top of the tonal range, while
    # shadows brighten (or deepen) the lower end.
    if adjusted > 0.65:
        ratio = (adjusted - 0.65) / 0.35
        adjusted += highlights * ratio
    elif adjusted < 0.35:
        ratio = (0.35 - adjusted) / 0.35
        adjusted += shadows * ratio

    # Contrast rotates the tone curve around the mid-point.
    adjusted = (adjusted - 0.5) * contrast_factor + 0.5

    # The black point slider lifts or sinks the darkest values.  Positive values
    # make blacks deeper, negative values raise the floor.
    if black_point > 0:
        adjusted -= black_point * (1.0 - adjusted)
    elif black_point < 0:
        adjusted -= black_point * adjusted

    return _clamp01(adjusted)


@jit(nopython=True, inline="always")
def _apply_color_transform(
    r: float,
    g: float,
    b: float,
    saturation: float,
    vibrance: float,
    cast: float,
    gain_r: float,
    gain_g: float,
    gain_b: float,
) -> tuple[float, float, float]:
    """Apply color adjustments to RGB channels."""
    mix_r = (1.0 - cast) + gain_r * cast
    mix_g = (1.0 - cast) + gain_g * cast
    mix_b = (1.0 - cast) + gain_b * cast
    r *= mix_r
    g *= mix_g
    b *= mix_b

    luma = 0.299 * r + 0.587 * g + 0.114 * b
    chroma_r = r - luma
    chroma_g = g - luma
    chroma_b = b - luma

    sat_amt = 1.0 + saturation
    vib_amt = 1.0 + vibrance
    w = 1.0 - _clamp(abs(luma - 0.5) * 2.0, 0.0, 1.0)
    chroma_scale = sat_amt * (1.0 + (vib_amt - 1.0) * w)
    chroma_r *= chroma_scale
    chroma_g *= chroma_scale
    chroma_b *= chroma_scale

    r = _clamp(luma + chroma_r, 0.0, 1.0)
    g = _clamp(luma + chroma_g, 0.0, 1.0)
    b = _clamp(luma + chroma_b, 0.0, 1.0)
    return r, g, b


@jit(nopython=True, inline="always")
def _apply_bw_channels(
    r: float,
    g: float,
    b: float,
    intensity: float,
    neutrals: float,
    tone: float,
    grain: float,
    noise: float,
) -> tuple[float, float, float]:
    """Return the transformed RGB triple for the Black & White effect."""

    intensity = _clamp01(intensity)
    neutrals = _clamp01(neutrals)
    tone = _clamp01(tone)
    grain = _clamp01(grain)
    noise = _clamp01(noise)

    luma = _clamp(0.2126 * r + 0.7152 * g + 0.0722 * b, 0.0, 1.0)

    soft_base = _clamp(pow(luma, 0.82), 0.0, 1.0)
    soft_curve = _contrast_tone_curve(soft_base, 0.0)
    g_soft = (soft_curve + soft_base) * 0.5
    g_neutral = luma
    g_rich = _contrast_tone_curve(_clamp(pow(luma, 1.0 / 1.22), 0.0, 1.0), 0.35)

    if intensity >= 0.5:
        blend = (intensity - 0.5) / 0.5
        gray = _mix(g_neutral, g_rich, blend)
    else:
        blend = (0.5 - intensity) / 0.5
        gray = _mix(g_soft, g_neutral, blend)

    gray = _gamma_neutral(gray, neutrals)
    gray = _contrast_tone_curve(gray, tone)

    if grain > 1e-6:
        gray += (noise - 0.5) * 0.2 * grain

    clamped = _clamp01(gray)
    return clamped, clamped, clamped


@jit(nopython=True, inline="always")
def _gamma_neutral(value: float, neutrals: float) -> float:
    """Return the neutral gamma adjustment matching the shader logic."""

    neutrals = _clamp01(neutrals)
    n = 0.6 * (neutrals - 0.5)
    gamma = math.pow(2.0, -n * 2.0)
    return _clamp(math.pow(_clamp(value, 0.0, 1.0), gamma), 0.0, 1.0)


@jit(nopython=True, inline="always")
def _contrast_tone_curve(value: float, tone: float) -> float:
    """Return the sigmoid tone adjustment used by ``BW_final.py``."""

    tone = _clamp01(tone)
    t = tone - 0.5
    factor = _mix(1.0, 2.2, t * 2.0) if t >= 0.0 else _mix(1.0, 0.6, -t * 2.0)
    x = _clamp(value, 0.0, 1.0)
    eps = 1e-6
    pos = _clamp(x, eps, 1.0 - eps)
    logit = math.log(pos / max(eps, 1.0 - pos))
    y = 1.0 / (1.0 + math.exp(-logit * factor))
    return _clamp(y, 0.0, 1.0)


@jit(nopython=True, inline="always")
def _grain_noise(x: int, y: int, width: int, height: int) -> float:
    """Return a deterministic pseudo random noise value in ``[0.0, 1.0]`` for grain."""

    if width <= 0 or height <= 0:
        return 0.5
    u = float(x) / float(max(width - 1, 1))
    v = float(y) / float(max(height - 1, 1))
    # Mirror the shader's ``rand`` function using a sine-based hash so the grain pattern stays
    # consistent across preview passes without requiring additional state.
    seed = u * 12.9898 + v * 78.233
    noise = math.sin(seed) * 43758.5453
    fraction = noise - math.floor(noise)
    return _clamp01(fraction)


@jit(nopython=True, inline="always")
def _mix(a: float, b: float, t: float) -> float:
    """Linear interpolation between a and b."""
    t = _clamp01(t)
    return a * (1.0 - t) + b * t


@jit(nopython=True, inline="always")
def _clamp01(value: float) -> float:
    """Clamp *value* to the inclusive ``[0.0, 1.0]`` range."""

    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


@jit(nopython=True, inline="always")
def _clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp *value* to the inclusive range [min_val, max_val]."""
    if value < min_val:
        return min_val
    if value > max_val:
        return max_val
    return value


@jit(nopython=True, inline="always")
def _float_to_uint8(value: float) -> int:
    """Convert *value* from ``[0.0, 1.0]`` to an 8-bit channel value."""

    scaled = round(value * 255.0)
    if scaled < 0:
        return 0
    if scaled > 255:
        return 255
    return int(scaled)
