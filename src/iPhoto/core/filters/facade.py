"""Facade module coordinating image adjustment executors.

This module provides the main public API for image adjustments, selecting
the appropriate executor based on available features and gracefully falling
back when needed.
"""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtGui import QImage

from ..color_resolver import ColorStats, compute_color_statistics
from .fallback_executor import apply_adjustments_fallback, apply_bw_using_qcolor
from .jit_executor import (
    apply_adjustments_fast_qimage,
    apply_color_adjustments_inplace_qimage,
)
from .numpy_executor import apply_bw_only
from .pillow_executor import apply_adjustments_with_lut, build_adjustment_lut


def _normalise_bw_param(value: float) -> float:
    """Return *value* mapped from legacy ``[-1, 1]`` to ``[0, 1]`` when needed."""

    numeric = float(value)
    if numeric < 0.0 or numeric > 1.0:
        numeric = (numeric + 1.0) * 0.5
    if numeric < 0.0:
        return 0.0
    if numeric > 1.0:
        return 1.0
    return numeric


def apply_adjustments(
    image: QImage,
    adjustments: Mapping[str, float],
    color_stats: ColorStats | None = None,
) -> QImage:
    """Return a new :class:`QImage` with *adjustments* applied.

    The function intentionally works on a copy of *image* so that the caller can
    reuse the original QImage as the immutable source of truth for subsequent
    recalculations.  Each adjustment operates on normalised channel intensities
    (``0.0`` - ``1.0``) and relies on simple tone curves so the preview remains
    responsive without requiring external numeric libraries.

    Parameters
    ----------
    image:
        The base image to transform.  The function accepts any format supported
        by :class:`QImage` and converts it to ``Format_ARGB32`` before applying
        the tone adjustments so per-pixel manipulation remains predictable.
    adjustments:
        Mapping of adjustment names (for example ``"Exposure"``) to floating
        point values in the ``[-1.0, 1.0]`` range.
    color_stats:
        Optional pre-computed color statistics for white balance. If not
        provided, will be computed when needed for color adjustments.
    """

    if image.isNull():
        return image

    # ``convertToFormat`` already returns a detached copy when the source image
    # has a different pixel format.  Cloning again would therefore waste memory
    # for the common case where a conversion is required, but performing a
    # ``copy()`` first would skip the optimisation entirely.  Converting once and
    # relying on Qt's copy-on-write semantics keeps the function efficient while
    # guaranteeing we never mutate the caller's instance in-place.
    result = image.convertToFormat(QImage.Format.Format_ARGB32)

    # ``convertToFormat`` can return a shallow copy that still references the
    # original pixel buffer when no conversion was required.  Creating an
    # explicit deep copy ensures Qt allocates a dedicated, writable buffer so
    # the fast adjustment path below never attempts to mutate a read-only view.
    # Without this defensive copy, edits made to previously cached images could
    # crash when the shared buffer exposes a read-only ``memoryview``.
    result = result.copy()

    # Extract and normalize all adjustment parameters
    brilliance = float(adjustments.get("Brilliance", 0.0))
    exposure = float(adjustments.get("Exposure", 0.0))
    highlights = float(adjustments.get("Highlights", 0.0))
    shadows = float(adjustments.get("Shadows", 0.0))
    brightness = float(adjustments.get("Brightness", 0.0))
    contrast = float(adjustments.get("Contrast", 0.0))
    black_point = float(adjustments.get("BlackPoint", 0.0))
    saturation = float(adjustments.get("Saturation", 0.0))
    vibrance = float(adjustments.get("Vibrance", 0.0))
    cast = float(adjustments.get("Cast", 0.0))
    gain_r = float(adjustments.get("Color_Gain_R", 1.0))
    gain_g = float(adjustments.get("Color_Gain_G", 1.0))
    gain_b = float(adjustments.get("Color_Gain_B", 1.0))
    gain_provided = (
        "Color_Gain_R" in adjustments
        or "Color_Gain_G" in adjustments
        or "Color_Gain_B" in adjustments
    )

    bw_flag = adjustments.get("BW_Enabled")
    if bw_flag is None:
        bw_flag = adjustments.get("BWEnabled")
    bw_enabled = bool(bw_flag)
    bw_intensity = _normalise_bw_param(
        adjustments.get("BW_Intensity", adjustments.get("BWIntensity", 0.5))
    )
    bw_neutrals = _normalise_bw_param(
        adjustments.get("BW_Neutrals", adjustments.get("BWNeutrals", 0.0))
    )
    bw_tone = _normalise_bw_param(
        adjustments.get("BW_Tone", adjustments.get("BWTone", 0.0))
    )
    bw_grain = _normalise_bw_param(
        adjustments.get("BW_Grain", adjustments.get("BWGrain", 0.0))
    )
    apply_bw = bw_enabled

    # Early exit if no adjustments are needed
    if all(
        abs(value) < 1e-6
        for value in (
            brilliance,
            exposure,
            highlights,
            shadows,
            brightness,
            contrast,
            black_point,
        )
    ) and all(abs(value) < 1e-6 for value in (saturation, vibrance)) and cast < 1e-6:
        if not apply_bw:
            # Nothing to do - return a cheap copy so callers still get a detached
            # instance they are free to mutate independently.
            return QImage(result)

    width = result.width()
    height = result.height()

    # ``exposure`` and ``brightness`` both affect overall luminance.  Treat the
    # exposure slider as a stronger variant so highlights bloom more quickly.
    exposure_term = exposure * 1.5
    brightness_term = brightness * 0.75

    # ``brilliance`` targets mid-tones while preserving highlights and deep
    # shadows.  Computing the strength once keeps the lookup-table builder
    # simple and avoids recalculating identical values inside tight loops.
    brilliance_strength = brilliance * 0.6

    # Pre-compute the contrast factor.  ``contrast`` is expressed as a delta
    # relative to the neutral slope of 1.0.
    contrast_factor = 1.0 + contrast

    # Try Pillow/LUT path first for tone adjustments (fastest for large images)
    lut = build_adjustment_lut(
        exposure_term,
        brightness_term,
        brilliance_strength,
        highlights,
        shadows,
        contrast_factor,
        black_point,
    )

    transformed = apply_adjustments_with_lut(result, lut)
    if transformed is not None:
        # Pillow path succeeded, now apply color and B&W if needed
        try:
            apply_color_adjustments_inplace_qimage(
                transformed,
                saturation,
                vibrance,
                cast,
                gain_r,
                gain_g,
                gain_b,
            )
        except (BufferError, RuntimeError, TypeError):
            # Color adjustment failed, apply color adjustments using fallback path
            apply_adjustments_fallback(
                transformed,
                transformed.width(),
                transformed.height(),
                exposure_term,
                brightness_term,
                brilliance_strength,
                highlights,
                shadows,
                contrast_factor,
                black_point,
                saturation,
                vibrance,
                cast,
                gain_r,
                gain_g,
                gain_b,
                False,  # Don't apply B&W here; handled below if needed
                0.0,    # bw_intensity
                0.0,    # bw_neutrals
                0.0,    # bw_tone
                0.0,    # bw_grain
            )

        if apply_bw:
            if not apply_bw_only(
                transformed,
                bw_intensity,
                bw_neutrals,
                bw_tone,
                bw_grain,
            ):
                # NumPy vectorized path failed, use QColor fallback
                apply_bw_using_qcolor(
                    transformed,
                    bw_intensity,
                    bw_neutrals,
                    bw_tone,
                    bw_grain,
                )
        return transformed

    # Pillow path not available, use JIT or fallback path
    bytes_per_line = result.bytesPerLine()

    # Compute color statistics if needed
    if color_stats is not None:
        gain_r, gain_g, gain_b = color_stats.white_balance_gain
    elif not gain_provided and (
        abs(saturation) > 1e-6
        or abs(vibrance) > 1e-6
        or cast > 1e-6
    ):
        color_stats = compute_color_statistics(result)
        gain_r, gain_g, gain_b = color_stats.white_balance_gain

    # Try JIT fast path
    try:
        apply_adjustments_fast_qimage(
            result,
            width,
            height,
            bytes_per_line,
            exposure_term,
            brightness_term,
            brilliance_strength,
            highlights,
            shadows,
            contrast_factor,
            black_point,
            saturation,
            vibrance,
            cast,
            gain_r,
            gain_g,
            gain_b,
            apply_bw,
            bw_intensity,
            bw_neutrals,
            bw_tone,
            bw_grain,
        )
    except (BufferError, RuntimeError, TypeError):
        # If the fast path fails we degrade gracefully to the slower, but very
        # reliable, QColor based implementation.  This keeps the editor usable
        # on platforms where the Qt binding exposes a read-only buffer or an
        # unsupported wrapper type.  The performance hit is preferable to a
        # crash that renders the feature unusable.
        apply_adjustments_fallback(
            result,
            width,
            height,
            exposure_term,
            brightness_term,
            brilliance_strength,
            highlights,
            shadows,
            contrast_factor,
            black_point,
            saturation,
            vibrance,
            cast,
            gain_r,
            gain_g,
            gain_b,
            apply_bw,
            bw_intensity,
            bw_neutrals,
            bw_tone,
            bw_grain,
        )

    return result
