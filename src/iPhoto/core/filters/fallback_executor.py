"""Fallback image adjustment executor using QColor.

This module provides a robust, platform-independent fallback implementation
that works even when buffer-based operations are not available. It's slower
but guaranteed to work across all Qt bindings.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QImage

from .algorithms import (
    _apply_bw_channels,
    _apply_channel_adjustments,
    _apply_color_transform,
    _grain_noise,
)


def apply_adjustments_fallback(
    image: QImage,
    width: int,
    height: int,
    exposure_term: float,
    brightness_term: float,
    brilliance_strength: float,
    highlights: float,
    shadows: float,
    contrast_factor: float,
    black_point: float,
    saturation: float,
    vibrance: float,
    cast: float,
    gain_r: float,
    gain_g: float,
    gain_b: float,
    apply_bw: bool,
    bw_intensity: float,
    bw_neutrals: float,
    bw_tone: float,
    bw_grain: float,
) -> None:
    """Slow but robust QColor-based tone mapping fallback.

    Using :class:`QColor` avoids direct buffer manipulation, which means it
    works even when the Qt binding cannot provide a writable pointer.  The
    function mirrors the fast path's tone mapping so both implementations yield
    identical visual output.
    """

    apply_color = abs(saturation) > 1e-6 or abs(vibrance) > 1e-6 or cast > 1e-6
    apply_bw_effect = apply_bw

    for y in range(height):
        for x in range(width):
            colour = image.pixelColor(x, y)

            r = _apply_channel_adjustments(
                colour.redF(),
                exposure_term,
                brightness_term,
                brilliance_strength,
                highlights,
                shadows,
                contrast_factor,
                black_point,
            )
            g = _apply_channel_adjustments(
                colour.greenF(),
                exposure_term,
                brightness_term,
                brilliance_strength,
                highlights,
                shadows,
                contrast_factor,
                black_point,
            )
            b = _apply_channel_adjustments(
                colour.blueF(),
                exposure_term,
                brightness_term,
                brilliance_strength,
                highlights,
                shadows,
                contrast_factor,
                black_point,
            )

            if apply_color:
                r, g, b = _apply_color_transform(
                    r,
                    g,
                    b,
                    saturation,
                    vibrance,
                    cast,
                    gain_r,
                    gain_g,
                    gain_b,
                )

            if apply_bw_effect:
                noise = 0.0
                if abs(bw_grain) > 1e-6:
                    noise = _grain_noise(x, y, width, height)
                r, g, b = _apply_bw_channels(
                    r,
                    g,
                    b,
                    bw_intensity,
                    bw_neutrals,
                    bw_tone,
                    bw_grain,
                    noise,
                )

            image.setPixelColor(x, y, QColor.fromRgbF(r, g, b, colour.alphaF()))


def apply_bw_using_qcolor(
    image: QImage,
    intensity: float,
    neutrals: float,
    tone: float,
    grain: float,
) -> None:
    """Fallback Black & White routine that relies on ``QColor`` accessors."""

    width = image.width()
    height = image.height()
    for y in range(height):
        for x in range(width):
            colour = image.pixelColor(x, y)
            r = colour.redF()
            g = colour.greenF()
            b = colour.blueF()
            noise = 0.5
            if abs(grain) > 1e-6:
                noise = _grain_noise(x, y, width, height)
            r, g, b = _apply_bw_channels(
                r,
                g,
                b,
                intensity,
                neutrals,
                tone,
                grain,
                noise,
            )
            image.setPixelColor(x, y, QColor.fromRgbF(r, g, b, colour.alphaF()))
