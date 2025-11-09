"""Tone mapping helpers powering the non-destructive edit pipeline."""

from __future__ import annotations

import math
from typing import Mapping, Sequence

import numpy as np
from numba import jit

from PySide6.QtGui import QImage, QColor

from ..utils.deps import load_pillow
from .light_resolver import LIGHT_KEYS
from .color_resolver import ColorStats, compute_color_statistics, _clamp

_PILLOW_SUPPORT = load_pillow()


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


# ``LIGHT_KEYS`` is re-exported from :mod:`iPhoto.core.light_resolver` so the constant lives in a
# single module.  The editing session, preview resolver, and sidecar IO layer all depend on the
# shared ordering when iterating over adjustment values, therefore duplicating the tuple here would
# risk subtle drift across the code base.


def apply_adjustments(
    image: QImage,
    adjustments: Mapping[str, float],
    color_stats: ColorStats | None = None,
) -> QImage:
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

    # ``convertToFormat`` can return a shallow copy that still references the
    # original pixel buffer when no conversion was required.  Creating an
    # explicit deep copy ensures Qt allocates a dedicated, writable buffer so
    # the fast adjustment path below never attempts to mutate a read-only view.
    # Without this defensive copy, edits made to previously cached images could
    # crash when the shared buffer exposes a read-only ``memoryview``.
    result = result.copy()

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
    bw_intensity = _normalise_bw_param(adjustments.get("BW_Intensity", adjustments.get("BWIntensity", 0.5)))
    bw_neutrals = _normalise_bw_param(adjustments.get("BW_Neutrals", adjustments.get("BWNeutrals", 0.0)))
    bw_tone = _normalise_bw_param(adjustments.get("BW_Tone", adjustments.get("BWTone", 0.0)))
    bw_grain = _normalise_bw_param(adjustments.get("BW_Grain", adjustments.get("BWGrain", 0.0)))
    apply_bw = bw_enabled

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
            # Nothing to do – return a cheap copy so callers still get a detached
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

    # A lookup table allows Pillow to apply the tone curve using C-optimised
    # routines, dramatically reducing the time spent on large full-resolution
    # images compared to the original Python double loop.  When Pillow is not
    # available we gracefully fall back to the legacy buffer walker.
    lut = _build_adjustment_lut(
        exposure_term,
        brightness_term,
        brilliance_strength,
        highlights,
        shadows,
        contrast_factor,
        black_point,
    )

    transformed = _apply_adjustments_with_lut(result, lut)
    if transformed is not None:
        _apply_color_adjustments_inplace_qimage(
            transformed,
            saturation,
            vibrance,
            cast,
            gain_r,
            gain_g,
            gain_b,
        )
        if apply_bw:
            _apply_bw_only(
                transformed,
                bw_intensity,
                bw_neutrals,
                bw_tone,
                bw_grain,
            )
        return transformed

    bytes_per_line = result.bytesPerLine()

    if color_stats is not None:
        gain_r, gain_g, gain_b = color_stats.white_balance_gain
    elif not gain_provided and (
        abs(saturation) > 1e-6
        or abs(vibrance) > 1e-6
        or cast > 1e-6
    ):
        color_stats = compute_color_statistics(result)
        gain_r, gain_g, gain_b = color_stats.white_balance_gain

    try:
        _apply_adjustments_fast_qimage(
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
        _apply_adjustments_fallback(
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


def _build_adjustment_lut(
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


def _apply_adjustments_with_lut(image: QImage, lut: Sequence[int]) -> QImage | None:
    """Attempt to transform *image* via a pre-computed lookup table."""

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


def _apply_adjustments_fast_qimage(
    image: QImage,
    width: int,
    height: int,
    bytes_per_line: int,
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
    """Mutate ``image`` in-place using the JIT-compiled adjustment kernel."""

    view, buffer_guard = _resolve_pixel_buffer(image)
    buffer_handle = buffer_guard
    _ = buffer_handle

    if getattr(view, "readonly", False):
        raise BufferError("QImage pixel buffer is read-only")

    if width <= 0 or height <= 0:
        return

    expected_size = bytes_per_line * height
    buffer = np.frombuffer(view, dtype=np.uint8, count=expected_size)
    if buffer.size < expected_size:
        raise BufferError("QImage pixel buffer is smaller than expected")

    apply_color = abs(saturation) > 1e-6 or abs(vibrance) > 1e-6 or cast > 1e-6

    _apply_adjustments_fast(
        buffer,
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
        apply_color,
        apply_bw,
        bw_intensity,
        bw_neutrals,
        bw_tone,
        bw_grain,
    )


@jit(nopython=True, cache=True)
def _apply_adjustments_fast(
    buffer: np.ndarray,
    width: int,
    height: int,
    bytes_per_line: int,
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
    apply_color: bool,
    apply_bw: bool,
    bw_intensity: float,
    bw_neutrals: float,
    bw_tone: float,
    bw_grain: float,
) -> None:
    if width <= 0 or height <= 0:
        return

    for y in range(height):
        row_offset = y * bytes_per_line
        for x in range(width):
            pixel_offset = row_offset + x * 4

            b = buffer[pixel_offset] / 255.0
            g = buffer[pixel_offset + 1] / 255.0
            r = buffer[pixel_offset + 2] / 255.0

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

            if apply_bw:
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

            buffer[pixel_offset] = _float_to_uint8(b)
            buffer[pixel_offset + 1] = _float_to_uint8(g)
            buffer[pixel_offset + 2] = _float_to_uint8(r)

def _apply_adjustments_fallback(
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


def _apply_color_adjustments_inplace_qimage(
    image: QImage,
    saturation: float,
    vibrance: float,
    cast: float,
    gain_r: float,
    gain_g: float,
    gain_b: float,
) -> None:
    if image.isNull():
        return

    apply_color = abs(saturation) > 1e-6 or abs(vibrance) > 1e-6 or cast > 1e-6
    if not apply_color:
        return

    view, guard = _resolve_pixel_buffer(image)
    buffer_handle = guard
    _ = buffer_handle

    if getattr(view, "readonly", False):
        raise BufferError("QImage pixel buffer is read-only")

    width = image.width()
    height = image.height()
    bytes_per_line = image.bytesPerLine()

    if width <= 0 or height <= 0:
        return

    expected_size = bytes_per_line * height
    buffer = np.frombuffer(view, dtype=np.uint8, count=expected_size)
    if buffer.size < expected_size:
        raise BufferError("QImage pixel buffer is smaller than expected")

    _apply_color_adjustments_inplace(
        buffer,
        width,
        height,
        bytes_per_line,
        saturation,
        vibrance,
        cast,
        gain_r,
        gain_g,
        gain_b,
    )


@jit(nopython=True, cache=True)
def _apply_color_adjustments_inplace(
    buffer: np.ndarray,
    width: int,
    height: int,
    bytes_per_line: int,
    saturation: float,
    vibrance: float,
    cast: float,
    gain_r: float,
    gain_g: float,
    gain_b: float,
) -> None:
    if width <= 0 or height <= 0:
        return

    apply_color = abs(saturation) > 1e-6 or abs(vibrance) > 1e-6 or cast > 1e-6
    if not apply_color:
        return

    for y in range(height):
        row_offset = y * bytes_per_line
        for x in range(width):
            pixel_offset = row_offset + x * 4
            b = buffer[pixel_offset] / 255.0
            g = buffer[pixel_offset + 1] / 255.0
            r = buffer[pixel_offset + 2] / 255.0

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

            buffer[pixel_offset] = _float_to_uint8(b)
            buffer[pixel_offset + 1] = _float_to_uint8(g)
            buffer[pixel_offset + 2] = _float_to_uint8(r)


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
    """Return a deterministic ``height`` × ``width`` pseudo-random field."""

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


def _apply_bw_vectorized(
    image: QImage,
    intensity: float,
    neutrals: float,
    tone: float,
    grain: float,
) -> bool:
    """Attempt to apply the Black & White effect using a fully vectorised path."""

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


def _apply_bw_only(
    image: QImage,
    intensity: float,
    neutrals: float,
    tone: float,
    grain: float,
) -> None:
    """Apply the Black & White pass to *image* in-place."""

    if image.isNull():
        return

    if _apply_bw_vectorized(image, intensity, neutrals, tone, grain):
        return

    # Some Qt bindings still expose read-only pixel buffers; in that scenario we
    # fall back to the dependable (albeit slower) QColor implementation so the
    # feature continues to work on every supported platform.
    _apply_bw_using_qcolor(image, intensity, neutrals, tone, grain)


def _apply_bw_using_qcolor(
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
    t = _clamp01(t)
    return a * (1.0 - t) + b * t


def _resolve_pixel_buffer(image: QImage) -> tuple[memoryview, object]:
    """Return a writable 1-D :class:`memoryview` over *image*'s pixels.

    Qt offers subtly different behaviours across bindings when exposing the
    raw pixel buffer.  PyQt returns a ``sip.voidptr`` that requires an explicit
    ``setsize`` call before Python can view the memory, while PySide exposes a
    ready-to-use ``memoryview`` instance.  Some downstream forks even ship
    stripped variants that only implement part of either API.  The helper keeps
    the fast path for modern PySide builds while gracefully falling back to the
    more verbose PyQt sequence, all without relying on private sip internals.

    The tuple's second element ensures the underlying Qt buffer stays alive for
    as long as the view is in scope.  Losing that reference allows the garbage
    collector to reclaim the temporary wrapper, which would corrupt future
    writes when Python keeps using the now-dangling ``memoryview``.
    """

    bytes_per_line = image.bytesPerLine()
    height = image.height()
    buffer = image.bits()
    expected_size = bytes_per_line * height

    # Preserve a reference to the original object so its lifetime matches the
    # returned memoryview.  The type differs across bindings (PySide's
    # ``memoryview`` vs. PyQt's ``sip.voidptr``) which is why the helper returns
    # it in the tuple.
    guard: object = buffer

    if isinstance(buffer, memoryview):
        view = buffer
    else:
        # PyQt requires ``setsize`` to expose the buffer length.  Only call the
        # method when it exists to avoid repeating the PySide crash that stemmed
        # from invoking the non-existent attribute.
        try:
            view = memoryview(buffer)
        except TypeError:
            if hasattr(buffer, "setsize"):
                buffer.setsize(expected_size)
                view = memoryview(buffer)
            else:
                raise RuntimeError("Unsupported QImage.bits() buffer wrapper") from None

    # Normalise the layout to unsigned bytes so per-channel offsets are
    # consistent regardless of the binding.  ``cast`` already returns ``self``
    # when the format matches, so there is no extra allocation on the fast path.
    try:
        view = view.cast("B")
    except TypeError:
        # Python < 3.12 expects the shape argument when recasting a multi-
        # dimensional memoryview.  Using the total number of bytes keeps the API
        # compatible with older interpreters that still ship with some Qt
        # distributions.
        view = view.cast("B", (view.nbytes,))

    if len(view) < expected_size:
        # Some bindings expose padding that is smaller than ``bytesPerLine`` ×
        # ``height``.  Restrict the view rather than risking out-of-bounds
        # writes.
        view = view[:expected_size]

    return view, guard


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
def _clamp01(value: float) -> float:
    """Clamp *value* to the inclusive ``[0.0, 1.0]`` range."""

    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


@jit(nopython=True, inline="always")
def _float_to_uint8(value: float) -> int:
    """Convert *value* from ``[0.0, 1.0]`` to an 8-bit channel value."""

    scaled = int(round(value * 255.0))
    if scaled < 0:
        return 0
    if scaled > 255:
        return 255
    return scaled
