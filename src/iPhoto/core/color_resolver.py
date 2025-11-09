"""Resolve Color adjustment values and analyse image statistics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping

import math

import numpy as np
from numba import jit

from PySide6.QtCore import Qt

try:  # pragma: no cover - availability depends on runtime environment
    from PySide6.QtGui import QImage
    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover - allows non-Qt environments to import the module
    QImage = Any  # type: ignore
    _QT_AVAILABLE = False


COLOR_KEYS = ("Saturation", "Vibrance", "Cast")
"""Canonical order for fine-grained Color adjustments."""

COLOR_RANGES: Mapping[str, tuple[float, float]] = {
    "Saturation": (-1.0, 1.0),
    "Vibrance": (-1.0, 1.0),
    "Cast": (0.0, 1.0),
}
"""Inclusive ranges for each Color adjustment slider."""


@dataclass(frozen=True)
class ColorStats:
    """Aggregate statistics describing the tonal distribution of an image."""

    saturation_mean: float = 0.35
    saturation_median: float = 0.30
    highlight_ratio: float = 0.10
    dark_ratio: float = 0.05
    skin_ratio: float = 0.10
    cast_magnitude: float = 0.0
    white_balance_gain: tuple[float, float, float] = (1.0, 1.0, 1.0)

    @classmethod
    def ensure(cls, stats: ColorStats | Mapping[str, float] | None) -> ColorStats:
        """Return *stats* as :class:`ColorStats`, falling back to defaults."""

        if stats is None:
            return cls()
        if isinstance(stats, cls):
            return stats
        return cls(
            saturation_mean=float(stats.get("saturation_mean", cls.saturation_mean)),
            saturation_median=float(stats.get("saturation_median", cls.saturation_median)),
            highlight_ratio=float(stats.get("highlight_ratio", cls.highlight_ratio)),
            dark_ratio=float(stats.get("dark_ratio", cls.dark_ratio)),
            skin_ratio=float(stats.get("skin_ratio", cls.skin_ratio)),
            cast_magnitude=float(stats.get("cast_magnitude", cls.cast_magnitude)),
            white_balance_gain=(
                float(stats.get("white_balance_gain_r", cls.white_balance_gain[0])),
                float(stats.get("white_balance_gain_g", cls.white_balance_gain[1])),
                float(stats.get("white_balance_gain_b", cls.white_balance_gain[2])),
            ),
        )


class ColorResolver:
    """Resolve Color adjustment vectors using image statistics."""

    @staticmethod
    def distribute_master(
        master: float,
        stats: ColorStats | Mapping[str, float] | None = None,
    ) -> dict[str, float]:
        """Return fine adjustments derived from the Color master slider."""

        stats_obj = ColorStats.ensure(stats)
        master_clamped = _clamp(master, -1.0, 1.0)

        k_hi = max(0.35, 1.0 - _smoothstep(0.02, 0.15, stats_obj.highlight_ratio))
        k_skin = 0.6 + 0.4 * (1.0 - _clamp(stats_obj.skin_ratio, 0.0, 1.0))
        k_sat0 = pow(max(0.0, 1.0 - stats_obj.saturation_median), 0.6)
        k_vib0 = max(0.0, 1.0 - stats_obj.saturation_mean)

        base_sat = 0.25 + 0.75 * k_sat0
        base_vib = 0.25 + 0.75 * k_vib0

        amp_sat, amp_vib = 1.6, 1.4
        sat = amp_sat * 0.9 * master_clamped * base_sat * k_hi * k_skin
        vib = amp_vib * 0.7 * master_clamped * base_vib * k_hi * k_skin

        cast_scale = 0.8 * abs(master_clamped) * _clamp(stats_obj.cast_magnitude / 0.4, 0.0, 1.0)
        cast = cast_scale

        epsilon = 0.01 * abs(master_clamped)
        if abs(sat) < epsilon:
            sat = math.copysign(epsilon, master_clamped)
        if abs(vib) < epsilon:
            vib = math.copysign(epsilon, master_clamped)

        return {
            "Saturation": _clamp(sat, *COLOR_RANGES["Saturation"]),
            "Vibrance": _clamp(vib, *COLOR_RANGES["Vibrance"]),
            "Cast": _clamp(cast, *COLOR_RANGES["Cast"]),
        }

    @staticmethod
    def calculate_master(
        saturation: float,
        vibrance: float,
        cast: float,
        *,
        stats: ColorStats | Mapping[str, float] | None = None,
    ) -> float:
        """Estimate the master slider value from resolved fine adjustments."""

        stats_obj = ColorStats.ensure(stats)
        if abs(saturation) < 1e-6 and abs(vibrance) < 1e-6 and abs(cast) < 1e-6:
            return 0.0

        positive_reference = ColorResolver.distribute_master(1.0, stats_obj)
        sat_ref = positive_reference.get("Saturation", 1.0)
        vib_ref = positive_reference.get("Vibrance", 1.0)
        cast_ref = max(positive_reference.get("Cast", 0.0), 1e-6)

        candidates: list[float] = []
        if abs(sat_ref) > 1e-6:
            candidates.append(saturation / sat_ref)
        if abs(vib_ref) > 1e-6:
            candidates.append(vibrance / vib_ref)

        sign_hint = sum(candidates)
        if abs(cast) > 1e-6:
            magnitude = abs(cast) / cast_ref
            if abs(sign_hint) < 1e-6:
                candidates.append(magnitude)
            else:
                candidates.append(math.copysign(magnitude, sign_hint))

        if not candidates:
            return 0.0

        averaged = sum(candidates) / len(candidates)
        return _clamp(averaged, -1.0, 1.0)

    @staticmethod
    def resolve_color_vector(
        master: float,
        overrides: Mapping[str, float] | None,
        *,
        stats: ColorStats | Mapping[str, float] | None = None,
        mode: str = "delta",
    ) -> dict[str, float]:
        """Combine the Color master slider with fine adjustment overrides."""

        stats_obj = ColorStats.ensure(stats)
        base = ColorResolver.distribute_master(master, stats_obj)
        overrides = overrides or {}
        resolved: MutableMapping[str, float] = dict(base)

        if mode == "delta":
            for key, value in overrides.items():
                if key in COLOR_KEYS:
                    minimum, maximum = COLOR_RANGES[key]
                    resolved[key] = _clamp(resolved.get(key, 0.0) + float(value), minimum, maximum)
        elif mode == "absolute":
            for key, value in overrides.items():
                if key in COLOR_KEYS:
                    minimum, maximum = COLOR_RANGES[key]
                    resolved[key] = _clamp(float(value), minimum, maximum)
        else:
            raise ValueError("mode must be 'delta' or 'absolute'")

        return dict(resolved)


def compute_color_statistics(image: QImage, *, max_sample_size: int = 1024) -> ColorStats:
    """Return :class:`ColorStats` describing *image*."""

    if not _QT_AVAILABLE:
        raise RuntimeError("Qt bindings are required to compute color statistics")

    if image.isNull():
        return ColorStats()

    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width = converted.width()
    height = converted.height()

    longest_edge = max(width, height)
    if longest_edge > max_sample_size:
        converted = converted.scaled(
            max_sample_size,
            max_sample_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        width = converted.width()
        height = converted.height()

    bytes_per_line = converted.bytesPerLine()
    buffer = converted.bits()

    view = memoryview(buffer)

    if width <= 0 or height <= 0:
        return ColorStats()

    buffer_array = np.frombuffer(view, dtype=np.uint8, count=bytes_per_line * height)
    try:
        surface = buffer_array.reshape((height, bytes_per_line))
    except ValueError:
        return ColorStats()
    pixel_region = surface[:, : width * 4].reshape((height, width, 4))

    bgr = pixel_region[..., :3].astype(np.float32, copy=False) / np.float32(255.0)
    b = bgr[..., 0]
    g = bgr[..., 1]
    r = bgr[..., 2]

    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    delta = max_c - min_c
    saturation = np.where(max_c <= 0.0, 0.0, delta / (max_c + 1e-8))
    value = max_c

    delta_safe = np.where(delta > 1e-8, delta, 1.0)
    hue = np.zeros_like(max_c)
    mask = delta > 1e-8
    mask_r = mask & (r == max_c)
    mask_g = mask & (g == max_c)
    mask_b = mask & (b == max_c)

    hue = np.where(mask_r, np.mod((g - b) / delta_safe, 6.0), hue)
    hue = np.where(mask_g, ((b - r) / delta_safe) + 2.0, hue)
    hue = np.where(mask_b, ((r - g) / delta_safe) + 4.0, hue)
    hue_deg = (hue / 6.0) * 360.0

    highlight_count = int(np.count_nonzero(value > 0.90))
    dark_count = int(np.count_nonzero(value < 0.05))
    skin_mask = (hue_deg > 10.0) & (hue_deg < 50.0) & (saturation > 0.1) & (saturation < 0.6)
    skin_count = int(np.count_nonzero(skin_mask))

    sum_saturation = float(np.sum(saturation, dtype=np.float64))

    bin_indices = np.clip((saturation * 64.0).astype(np.int64), 0, 63)
    hist = np.bincount(bin_indices.ravel(), minlength=64)

    lin_r = _srgb_to_linear(r)
    lin_g = _srgb_to_linear(g)
    lin_b = _srgb_to_linear(b)

    sum_lin_r = float(np.sum(lin_r, dtype=np.float64))
    sum_lin_g = float(np.sum(lin_g, dtype=np.float64))
    sum_lin_b = float(np.sum(lin_b, dtype=np.float64))

    count = width * height

    if count == 0:
        return ColorStats()

    mean_saturation = sum_saturation / count
    cumulative = np.cumsum(hist)
    median_target = count // 2
    median_index = int(np.searchsorted(cumulative, median_target, side="left"))
    if median_index >= hist.size:
        median_index = hist.size - 1
    median_saturation = (median_index + 0.5) / 64.0

    highlight_ratio = highlight_count / count
    dark_ratio = dark_count / count
    skin_ratio = skin_count / count

    avg_lin_r = sum_lin_r / count
    avg_lin_g = sum_lin_g / count
    avg_lin_b = sum_lin_b / count
    avg_lin = (avg_lin_r + avg_lin_g + avg_lin_b) / 3.0
    cast_magnitude = max(
        abs(avg_lin_r - avg_lin),
        abs(avg_lin_g - avg_lin),
        abs(avg_lin_b - avg_lin),
    )

    gain_r = avg_lin / avg_lin_r if avg_lin_r > 1e-6 else 1.0
    gain_g = avg_lin / avg_lin_g if avg_lin_g > 1e-6 else 1.0
    gain_b = avg_lin / avg_lin_b if avg_lin_b > 1e-6 else 1.0

    gain_r = _clamp(gain_r, 0.5, 2.5)
    gain_g = _clamp(gain_g, 0.5, 2.5)
    gain_b = _clamp(gain_b, 0.5, 2.5)

    return ColorStats(
        saturation_mean=_clamp(mean_saturation, 0.0, 1.0),
        saturation_median=_clamp(median_saturation, 0.0, 1.0),
        highlight_ratio=_clamp(highlight_ratio, 0.0, 1.0),
        dark_ratio=_clamp(dark_ratio, 0.0, 1.0),
        skin_ratio=_clamp(skin_ratio, 0.0, 1.0),
        cast_magnitude=_clamp(cast_magnitude, 0.0, 1.0),
        white_balance_gain=(gain_r, gain_g, gain_b),
    )


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 <= edge0:
        return 0.0
    t = (x - edge0) / (edge1 - edge0)
    t = _clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


@jit(nopython=True, inline="always")
def _clamp(value: float, minimum: float, maximum: float) -> float:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _srgb_to_linear(channel: float | np.ndarray) -> float | np.ndarray:
    """Return *channel* converted from sRGB to linear space."""

    if np.isscalar(channel):
        channel_float = float(channel)
        if channel_float <= 0.04045:
            return channel_float / 12.92
        return pow((channel_float + 0.055) / 1.055, 2.4)

    array = np.asarray(channel, dtype=np.float32)
    linear = np.where(
        array <= 0.04045,
        array / 12.92,
        np.power((array + 0.055) / 1.055, 2.4, dtype=np.float32),
    )
    return linear


__all__ = [
    "COLOR_KEYS",
    "COLOR_RANGES",
    "ColorResolver",
    "ColorStats",
    "compute_color_statistics",
]
