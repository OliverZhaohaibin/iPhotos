"""Tone mapping helpers powering the non-destructive edit pipeline."""

from __future__ import annotations

import math
from typing import Mapping, Sequence


from PySide6.QtGui import QImage, QColor

from ..utils.deps import load_pillow
from .light_resolver import LIGHT_KEYS
from .color_resolver import ColorStats, compute_color_statistics, _clamp

_PILLOW_SUPPORT = load_pillow()


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

    bw_enabled = bool(adjustments.get("BW_Enabled", adjustments.get("BWEnabled", True)))
    bw_intensity = float(adjustments.get("BW_Intensity", adjustments.get("BWIntensity", 0.5)))
    bw_neutrals = float(adjustments.get("BW_Neutrals", adjustments.get("BWNeutrals", 0.0)))
    bw_tone = float(adjustments.get("BW_Tone", adjustments.get("BWTone", 0.0)))
    bw_grain = float(adjustments.get("BW_Grain", adjustments.get("BWGrain", 0.0)))
    apply_bw = bw_enabled and (
        abs(bw_intensity - 0.5) > 1e-6 or abs(bw_neutrals) > 1e-6 or abs(bw_tone) > 1e-6 or abs(bw_grain) > 1e-6
    )

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
        _apply_color_adjustments_inplace(
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
        _apply_adjustments_fast(
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


def _apply_adjustments_fast(
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
    """Mutate ``image`` in-place using direct pixel buffer access.

    The helper keeps the tight loop isolated so :func:`apply_adjustments` can
    fall back to a slower implementation when the buffer is not writable.
    """

    view, buffer_guard = _resolve_pixel_buffer(image)

    # Keep an explicit reference to the guard so the Qt wrapper that exposes
    # the pixel buffer stays alive for the duration of the processing loop.
    buffer_handle = buffer_guard
    _ = buffer_handle

    if getattr(view, "readonly", False):
        raise BufferError("QImage pixel buffer is read-only")

    apply_color = abs(saturation) > 1e-6 or abs(vibrance) > 1e-6 or cast > 1e-6
    apply_bw_effect = apply_bw

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
                r, g, b = _apply_bw_effect_scalar(
                    r,
                    g,
                    b,
                    bw_intensity,
                    bw_neutrals,
                    bw_tone,
                    bw_grain,
                    x,
                    y,
                    width,
                    height,
                )

            view[pixel_offset] = _float_to_uint8(b)
            view[pixel_offset + 1] = _float_to_uint8(g)
            view[pixel_offset + 2] = _float_to_uint8(r)
            # The alpha channel (``pixel_offset + 3``) is intentionally left
            # untouched so transparent assets retain their original opacity.


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
                r, g, b = _apply_bw_effect_scalar(
                    r,
                    g,
                    b,
                    bw_intensity,
                    bw_neutrals,
                    bw_tone,
                    bw_grain,
                    x,
                    y,
                    width,
                    height,
                )

            image.setPixelColor(x, y, QColor.fromRgbF(r, g, b, colour.alphaF()))


def _apply_color_adjustments_inplace(
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

    for y in range(height):
        row_offset = y * bytes_per_line
        for x in range(width):
            pixel_offset = row_offset + x * 4
            b = view[pixel_offset] / 255.0
            g = view[pixel_offset + 1] / 255.0
            r = view[pixel_offset + 2] / 255.0

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

            view[pixel_offset] = _float_to_uint8(b)
            view[pixel_offset + 1] = _float_to_uint8(g)
            view[pixel_offset + 2] = _float_to_uint8(r)


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
    if abs(intensity) <= 1e-6 and abs(grain) <= 1e-6:
        # Mirror the shader's early exit so neutral parameter values do not waste cycles.
        return

    width = image.width()
    height = image.height()
    bytes_per_line = image.bytesPerLine()

    try:
        view, guard = _resolve_pixel_buffer(image)
    except (BufferError, RuntimeError, TypeError):
        view = None
        guard = None

    if view is None or getattr(view, "readonly", False):
        _apply_bw_using_qcolor(image, intensity, neutrals, tone, grain)
        return

    buffer_guard = guard
    _ = buffer_guard

    for y in range(height):
        row_offset = y * bytes_per_line
        for x in range(width):
            pixel_offset = row_offset + x * 4
            b = view[pixel_offset] / 255.0
            g = view[pixel_offset + 1] / 255.0
            r = view[pixel_offset + 2] / 255.0
            r, g, b = _apply_bw_effect_scalar(
                r,
                g,
                b,
                intensity,
                neutrals,
                tone,
                grain,
                x,
                y,
                width,
                height,
            )
            view[pixel_offset] = _float_to_uint8(b)
            view[pixel_offset + 1] = _float_to_uint8(g)
            view[pixel_offset + 2] = _float_to_uint8(r)


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
            r, g, b = _apply_bw_effect_scalar(
                r,
                g,
                b,
                intensity,
                neutrals,
                tone,
                grain,
                x,
                y,
                width,
                height,
            )
            image.setPixelColor(x, y, QColor.fromRgbF(r, g, b, colour.alphaF()))


def _apply_bw_effect_scalar(
    r: float,
    g: float,
    b: float,
    intensity: float,
    neutrals: float,
    tone: float,
    grain: float,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[float, float, float]:
    """Return a grayscale triple that mirrors the GPU Black & White effect."""

    intensity = _clamp01(intensity)
    neutrals = _clamp01(neutrals)
    tone = _clamp01(tone)
    grain_amount = _clamp01(grain)

    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    base = _clamp01(luma)

    g_neutral = base
    g_soft_base = math.pow(base, 0.82)
    g_soft = (_bw_contrast_tone(g_soft_base, 0.0) + g_soft_base) * 0.5
    g_rich = _bw_contrast_tone(math.pow(base, 1.0 / 1.22), 0.35)

    if intensity >= 0.5:
        mix_factor = (intensity - 0.5) / 0.5
        gray = _mix(g_neutral, g_rich, mix_factor)
    else:
        mix_factor = (0.5 - intensity) / 0.5
        gray = _mix(g_soft, g_neutral, mix_factor)

    gray = _bw_gamma_neutral(gray, neutrals)
    gray = _bw_contrast_tone(gray, tone)

    if grain_amount > 1e-6:
        gray += _grain_noise(x, y, width, height) * grain_amount

    gray = _clamp01(gray)
    return gray, gray, gray


def _bw_gamma_neutral(value: float, neutral_amount: float) -> float:
    """Apply the neutral gamma tweak used by the GLSL shader."""

    n = 0.6 * (neutral_amount - 0.5)
    gamma = math.pow(2.0, -n * 2.0)
    return math.pow(_clamp01(value), gamma)


def _bw_contrast_tone(value: float, tone_amount: float) -> float:
    """Apply the tone S-curve used by the GLSL shader."""

    t = tone_amount - 0.5
    slope = _mix(1.0, 2.2, t * 2.0) if t >= 0.0 else _mix(1.0, 0.6, -t * 2.0)
    x = _clamp01(value)
    eps = 1e-6
    x_safe = max(eps, min(1.0 - eps, x))
    logit = math.log(x_safe / (1.0 - x_safe))
    y = 1.0 / (1.0 + math.exp(-logit * slope))
    return _clamp01(y)


def _grain_noise(x: int, y: int, width: int, height: int) -> float:
    """Return a deterministic pseudo random noise value in ``[-0.1, 0.1]``."""

    if width <= 0 or height <= 0:
        return 0.0
    u = float(x) / float(max(width - 1, 1))
    v = float(y) / float(max(height - 1, 1))
    # Mirror the shader's ``rand`` function using a sine-based hash so the grain pattern stays
    # consistent across preview passes without requiring additional state.
    seed = u * 12.9898 + v * 78.233
    noise = math.sin(seed) * 43758.5453
    fraction = noise - math.floor(noise)
    return (fraction - 0.5) * 0.2


def _mix(a: float, b: float, t: float) -> float:
    t = _clamp01(t)
    return a * (1.0 - t) + b * t


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


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
