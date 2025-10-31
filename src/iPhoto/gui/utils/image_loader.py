"""Helpers for loading Qt image primitives with Pillow fallbacks."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPixmap

from ...utils.deps import load_pillow

_PILLOW = load_pillow()
if _PILLOW is not None:  # pragma: no branch - import guard
    _Image = _PILLOW.Image
    _ImageOps = _PILLOW.ImageOps
    _ImageQt = _PILLOW.ImageQt
else:  # pragma: no cover - executed when Pillow is unavailable
    _Image = None  # type: ignore[assignment]
    _ImageOps = None  # type: ignore[assignment]
    _ImageQt = None  # type: ignore[assignment]


def load_qimage(source: Path, target: QSize | None = None) -> Optional[QImage]:
    """Return a :class:`QImage` for *source* with optional scaling."""

    # ``QImageReader`` is most efficient when it can stream directly from the
    # filename because many formats (JPEG, HEIC, etc.) expose fast-paths for
    # downscaling during decode.  Reading the bytes eagerly would defeat those
    # optimisations, so we prefer to hand the path to Qt and only fall back to
    # Pillow if decoding fails entirely.
    reader = QImageReader(str(source))
    # Qt maintains a process-wide image cache that is enabled by default.
    # Large libraries can end up decoding hundreds of images during a single
    # browsing session which would otherwise accumulate in that cache.  The
    # additional allocations not only increase peak memory usage but can also
    # hold operating system file handles open.  Disabling the cache keeps the
    # application's memory footprint predictable and avoids lingering locks on
    # the original media files while still allowing higher level components to
    # implement their own caching policies.
    reader.setCacheEnabled(False)
    reader.setAutoTransform(True)
    if target is not None and target.isValid() and not target.isEmpty():
        original_size = reader.size()
        if original_size.isValid() and not original_size.isEmpty():
            # ``QImageReader.setScaledSize`` always interprets the requested
            # dimensions literally, even when that would distort the image.  We
            # pre-compute a size that preserves the source aspect ratio so the
            # decoder performs a proportional downscale rather than stretching to
            # fill the viewport bounds supplied by the caller.
            scaled_target = original_size.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            # Only request scaling when the destination is genuinely smaller; this
            # avoids unnecessary interpolation for thumbnails that are already
            # below the desired output resolution.
            if (
                scaled_target.width() < original_size.width()
                or scaled_target.height() < original_size.height()
            ):
                reader.setScaledSize(scaled_target)
        else:
            # Some formats only disclose their intrinsic size during ``read``. In
            # those cases we skip ``setScaledSize`` entirely to avoid guessing an
            # aspect ratio that might be wildly incorrect.  The caller will still
            # downscale the resulting pixmap once Qt reports the true dimensions.
            pass
    image = reader.read()
    if not image.isNull():
        return image
    return _load_with_pillow(source, target)


def load_qpixmap(source: Path, target: QSize | None = None) -> Optional[QPixmap]:
    """Return a :class:`QPixmap` for *source*, falling back to Pillow when required."""

    image = load_qimage(source, target)
    if image is None or image.isNull():
        return None
    pixmap = QPixmap.fromImage(image)
    if pixmap.isNull():
        return None
    return pixmap


def qimage_from_bytes(data: bytes) -> Optional[QImage]:
    """Return a :class:`QImage` decoded from JPEG/PNG *data*."""

    image = QImage()
    if image.loadFromData(data):
        return image
    if image.loadFromData(data, "JPEG"):
        return image
    if image.loadFromData(data, "JPG"):
        return image
    if image.loadFromData(data, "PNG"):
        return image
    if _Image is None or _ImageOps is None or _ImageQt is None:
        return None
    try:
        with _Image.open(BytesIO(data)) as img:  # type: ignore[union-attr]
            img = _ImageOps.exif_transpose(img)
            qt_image = _ImageQt(img.convert("RGBA"))
    except Exception:  # pragma: no cover - Pillow failures are soft
        return None
    return QImage(qt_image)


def _load_with_pillow(source: Path, target: QSize | None = None) -> Optional[QImage]:
    if _Image is None or _ImageOps is None or _ImageQt is None:
        return None
    try:
        with _Image.open(source) as img:  # type: ignore[attr-defined]
            img = _ImageOps.exif_transpose(img)  # type: ignore[attr-defined]
            if target is not None and target.isValid() and not target.isEmpty():
                resample = getattr(_Image, "Resampling", _Image)
                resample_filter = getattr(resample, "LANCZOS", _Image.BICUBIC)
                img.thumbnail((target.width(), target.height()), resample_filter)
            qt_image = _ImageQt(img.convert("RGBA"))  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - Pillow loader failure propagates softly
        return None
    return QImage(qt_image)
