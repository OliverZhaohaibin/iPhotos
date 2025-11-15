"""NumPy vectorized executor for black & white effects.

This module provides efficient vectorized implementations of B&W effects
using NumPy operations for better performance on large images.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtGui import QImage

from .utils import _resolve_pixel_buffer


def _bw_unsigned_to_signed(value: float) -> float:
    """Return *value* remapped from ``[0, 1]`` into the signed ``[-1, 1]`` domain."""

    numeric = float(value)
    return float(max(-1.0, min(1.0, numeric * 2.0 - 1.0)))


def _np_mix(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """Vectorised equivalent of GLSL's ``mix`` helper."""

    return a * (1.0 - t) + b * t


def _np_gamma_neutral_signed(gray: np.ndarray, neutral_adjust: float) -> np.ndarray:
    """Apply the signed neutral gamma curve used by the shader to ``gray``."""

    neutral = float(max(-1.0, min(1.0, neutral_adjust)))
    magnitude = 0.6 * abs(neutral)
    gamma = math.pow(2.0, -magnitude) if neutral >= 0.0 else math.pow(2.0, magnitude)
    clamped = np.clip(gray, 0.0, 1.0).astype(np.float32, copy=False)
    np.power(clamped, gamma, out=clamped)
    return np.clip(clamped, 0.0, 1.0)


def _np_contrast_tone_signed(gray: np.ndarray, tone_adjust: float) -> np.ndarray:
    """Apply the signed logistic tone curve to ``gray``."""

    tone_value = float(max(-1.0, min(1.0, tone_adjust)))
    if tone_value >= 0.0:
        k = 1.0 + (2.2 - 1.0) * tone_value
    else:
        k = 1.0 + (0.6 - 1.0) * -tone_value

    x = np.clip(gray, 0.0, 1.0).astype(np.float32, copy=False)
    epsilon = 1e-6
    clamped = np.clip(x, epsilon, 1.0 - epsilon)
    logit = np.log(clamped / np.clip(1.0 - clamped, epsilon, 1.0))
    result = 1.0 / (1.0 + np.exp(-logit * k))
    return np.clip(result.astype(np.float32, copy=False), 0.0, 1.0)


def _generate_grain_field(width: int, height: int) -> np.ndarray:
    """Return a deterministic ``height`` x ``width`` pseudo-random field."""

    if width <= 0 or height <= 0:
        return np.zeros((max(1, height), max(1, width)), dtype=np.float32)

    x = np.arange(width, dtype=np.float32)
    y = np.arange(height, dtype=np.float32)
    if width > 1:
        u = x / float(width - 1)
    else:
        u = np.zeros_like(x)
    if height > 1:
        v = y / float(height - 1)
    else:
        v = np.zeros_like(y)

    seed = u[None, :] * np.float32(12.9898) + v[:, None] * np.float32(78.233)
    noise = np.sin(seed).astype(np.float32, copy=False) * np.float32(43758.5453)
    fraction = noise - np.floor(noise)
    return np.clip(fraction.astype(np.float32), 0.0, 1.0)


def apply_bw_vectorized(
    image: QImage,
    intensity: float,
    neutrals: float,
    tone: float,
    grain: float,
) -> bool:
    """Attempt to apply the Black & White effect using a fully vectorised path.

    Returns True on success, False if vectorization fails and fallback is needed.
    """

    width = image.width()
    height = image.height()
    bytes_per_line = image.bytesPerLine()

    if width <= 0 or height <= 0:
        return True

    try:
        view, guard = _resolve_pixel_buffer(image)
    except (BufferError, RuntimeError, TypeError):
        return False

    if getattr(view, "readonly", False):
        return False

    buffer_guard = guard
    # Holding a reference to ``guard`` keeps the Qt wrapper that owns the raw pixel
    # buffer alive while NumPy operates on the exported memoryview.
    _ = buffer_guard

    buffer = np.frombuffer(view, dtype=np.uint8, count=bytes_per_line * height)
    try:
        surface = buffer.reshape((height, bytes_per_line))
    except ValueError:
        return False

    rgb_region = surface[:, : width * 4].reshape((height, width, 4))

    bgr = rgb_region[..., :3].astype(np.float32, copy=False)
    rgb = bgr[:, :, ::-1] / np.float32(255.0)

    intensity_signed = _bw_unsigned_to_signed(intensity)
    neutrals_signed = _bw_unsigned_to_signed(neutrals)
    tone_signed = _bw_unsigned_to_signed(tone)
    grain_amount = float(max(0.0, min(1.0, grain)))

    if (
        abs(intensity_signed) <= 1e-6
        and abs(neutrals_signed) <= 1e-6
        and abs(tone_signed) <= 1e-6
        and grain_amount <= 1e-6
    ):
        return True

    luma = (
        rgb[:, :, 0] * 0.2126
        + rgb[:, :, 1] * 0.7152
        + rgb[:, :, 2] * 0.0722
    ).astype(np.float32)

    luma_clamped = np.clip(luma, 0.0, 1.0).astype(np.float32, copy=False)
    g_soft = np.power(luma_clamped, 0.85).astype(np.float32, copy=False)
    g_neutral = luma
    g_rich = _np_contrast_tone_signed(luma, 0.35)

    if intensity_signed >= 0.0:
        gray = _np_mix(g_neutral, g_rich, intensity_signed)
    else:
        gray = _np_mix(g_soft, g_neutral, intensity_signed + 1.0)

    gray = _np_gamma_neutral_signed(gray, neutrals_signed)
    gray = _np_contrast_tone_signed(gray, tone_signed)

    if grain_amount > 1e-6:
        noise = _generate_grain_field(width, height)
        gray = gray + (noise - 0.5) * 0.2 * grain_amount

    gray = np.clip(gray, 0.0, 1.0).astype(np.float32, copy=False)
    gray_bytes = np.rint(gray * np.float32(255.0)).astype(np.uint8)

    rgb_region[..., 0] = gray_bytes
    rgb_region[..., 1] = gray_bytes
    rgb_region[..., 2] = gray_bytes

    return True


def apply_bw_only(
    image: QImage,
    intensity: float,
    neutrals: float,
    tone: float,
    grain: float,
) -> bool:
    """Apply the Black & White pass to *image* in-place.

    Returns True if successfully applied, False if fallback is needed.
    """

    if image.isNull():
        return True

    return apply_bw_vectorized(image, intensity, neutrals, tone, grain)
