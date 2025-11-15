"""Pillow-based image adjustment executor using lookup tables (LUT).

This module provides fast tone adjustments using Pillow's C-optimized LUT
application, which is particularly efficient for large images.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtGui import QImage

from ...utils.deps import load_pillow
from .algorithms import _apply_channel_adjustments, _float_to_uint8
from .utils import _resolve_pixel_buffer

_PILLOW_SUPPORT = load_pillow()


def build_adjustment_lut(
    exposure: float,
    brightness: float,
    brilliance: float,
    highlights: float,
    shadows: float,
    contrast_factor: float,
    black_point: float,
) -> list[int]:
    """Pre-compute the tone curve for every possible 8-bit channel value."""

    lut: list[int] = []
    for channel_value in range(256):
        normalised = channel_value / 255.0
        adjusted = _apply_channel_adjustments(
            normalised,
            exposure,
            brightness,
            brilliance,
            highlights,
            shadows,
            contrast_factor,
            black_point,
        )
        lut.append(_float_to_uint8(adjusted))
    return lut


def apply_adjustments_with_lut(image: QImage, lut: Sequence[int]) -> QImage | None:
    """Attempt to transform *image* via a pre-computed lookup table.

    Returns None if Pillow is not available or if processing fails.
    """

    support = _PILLOW_SUPPORT
    if support is None or support.Image is None or support.ImageQt is None:
        return None

    try:
        width = image.width()
        height = image.height()
        bytes_per_line = image.bytesPerLine()

        # ``_resolve_pixel_buffer`` already performs the heavy lifting required
        # to expose a contiguous ``memoryview`` over the QImage data across the
        # various Qt/Python binding permutations.  Reusing it avoids the
        # ``setsize`` AttributeError that PySide raises (and which previously
        # forced us down the slow fallback path).
        view, buffer_guard = _resolve_pixel_buffer(image)

        # Pillow is only interested in the raw byte sequence and copies it once
        # we immediately call ``copy()`` on the resulting image.  Passing the
        # ``memoryview`` directly therefore avoids an intermediate ``bytes``
        # allocation while the guard keeps the underlying Qt wrapper alive long
        # enough for Pillow to finish its own copy.
        buffer = view if isinstance(view, memoryview) else memoryview(view)
        guard = buffer_guard
        _ = guard  # Explicitly anchor the guard for the duration of the call.
        pil_image = support.Image.frombuffer(
            "RGBA",
            (width, height),
            buffer,
            "raw",
            "BGRA",
            bytes_per_line,
            1,
        ).copy()

        # ``Image.point`` applies per-channel lookup tables in native code.  We
        # reuse the same curve for RGB while preserving the alpha channel via an
        # identity table to ensure transparency remains untouched.
        alpha_table = list(range(256))
        table: list[int] = list(lut) * 3 + alpha_table
        pil_image = pil_image.point(table)

        qt_image = QImage(support.ImageQt(pil_image))
        if qt_image.format() != QImage.Format.Format_ARGB32:
            qt_image = qt_image.convertToFormat(QImage.Format.Format_ARGB32)
        return qt_image
    except Exception:
        # Pillow is optional; if anything goes wrong we fall back to the
        # original buffer-walking implementation.
        return None
