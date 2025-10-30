"""Tone mapping helpers powering the non-destructive edit pipeline."""

from __future__ import annotations

from typing import Mapping

from PySide6.QtGui import QImage


# Mapping keys used throughout the editing pipeline.  The constants make it easy to
# keep :mod:`iPhoto.io.sidecar`, :mod:`iPhoto.gui.models.edit_session`, and the
# rendering code in sync without scattering literal strings across the code base.
LIGHT_KEYS = (
    "Brilliance",
    "Exposure",
    "Highlights",
    "Shadows",
    "Brightness",
    "Contrast",
    "BlackPoint",
)
"""Canonical order of the light adjustment keys."""


def apply_adjustments(image: QImage, adjustments: Mapping[str, float]) -> QImage:
    """Return a new :class:`QImage` with *adjustments* applied.

    The function intentionally works on a copy of *image* so that the caller can
    reuse the original QImage as the immutable source of truth for subsequent
    recalculations.  Each adjustment operates on normalised channel intensities
    (``0.0`` – ``1.0``) and relies on simple tone curves so the preview remains
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

    brilliance = float(adjustments.get("Brilliance", 0.0))
    exposure = float(adjustments.get("Exposure", 0.0))
    highlights = float(adjustments.get("Highlights", 0.0))
    shadows = float(adjustments.get("Shadows", 0.0))
    brightness = float(adjustments.get("Brightness", 0.0))
    contrast = float(adjustments.get("Contrast", 0.0))
    black_point = float(adjustments.get("BlackPoint", 0.0))

    if all(abs(value) < 1e-6 for value in (
        brilliance,
        exposure,
        highlights,
        shadows,
        brightness,
        contrast,
        black_point,
    )):
        # Nothing to do – return a cheap copy so callers still get a detached
        # instance they are free to mutate independently.
        return QImage(result)

    width = result.width()
    height = result.height()

    # ``exposure`` and ``brightness`` both affect overall luminance.  Treat the
    # exposure slider as a stronger variant so highlights bloom more quickly.
    exposure_term = exposure * 1.5
    brightness_term = brightness * 0.75

    # ``brilliance`` targets mid-tones.  A positive value brightens mid-tones
    # while a negative value deepens them.  Compute the strength once so it can
    # be reused across channels.
    brilliance_strength = brilliance * 0.6

    # Pre-compute the contrast factor.  ``contrast`` is expressed as a delta
    # relative to the neutral slope of 1.0.
    contrast_factor = 1.0 + contrast

    # Access the raw pixel buffer once and work with it directly.  Using
    # :class:`QColor` for each pixel triggers a large amount of Python ↔ Qt
    # marshalling overhead which quickly becomes noticeable when the user drags
    # a slider.  The bytes are laid out as BGRA because ``Format_ARGB32`` stores
    # 32-bit integers in little-endian order.
    buffer = result.bits()
    bytes_per_line = result.bytesPerLine()

    # ``QImage.bits`` returns a ``memoryview`` in PySide6 whose size already
    # matches the backing store.  Some Qt bindings expose ``setsize`` to resize
    # the view explicitly, but PySide6 intentionally omits the API which caused
    # an ``AttributeError`` to be raised when the preview refreshed.  Casting to
    # unsigned bytes gives us predictable indexing across platforms without the
    # now-unsupported ``setsize`` call.
    view = buffer.cast("B") if isinstance(buffer, memoryview) else memoryview(buffer).cast("B")

    for y in range(height):
        row_offset = y * bytes_per_line
        for x in range(width):
            pixel_offset = row_offset + x * 4

            b = view[pixel_offset] / 255.0
            g = view[pixel_offset + 1] / 255.0
            r = view[pixel_offset + 2] / 255.0

            r = _apply_channel_adjustments(
                r,
                exposure_term,
                brightness_term,
                brilliance_strength,
                highlights,
                shadows,
                contrast_factor,
                black_point,
            )
            g = _apply_channel_adjustments(
                g,
                exposure_term,
                brightness_term,
                brilliance_strength,
                highlights,
                shadows,
                contrast_factor,
                black_point,
            )
            b = _apply_channel_adjustments(
                b,
                exposure_term,
                brightness_term,
                brilliance_strength,
                highlights,
                shadows,
                contrast_factor,
                black_point,
            )

            view[pixel_offset] = _float_to_uint8(b)
            view[pixel_offset + 1] = _float_to_uint8(g)
            view[pixel_offset + 2] = _float_to_uint8(r)
            # Alpha (view[pixel_offset + 3]) is preserved as-is.

    return result


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


def _clamp01(value: float) -> float:
    """Clamp *value* to the inclusive ``[0.0, 1.0]`` range."""

    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _float_to_uint8(value: float) -> int:
    """Convert *value* from ``[0.0, 1.0]`` to an 8-bit channel value."""

    scaled = int(round(value * 255.0))
    if scaled < 0:
        return 0
    if scaled > 255:
        return 255
    return scaled
