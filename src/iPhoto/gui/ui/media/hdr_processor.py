"""Helpers that normalise HDR video frames for SDR-only displays."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject
from PySide6.QtGui import QColorSpace, QImage

try:  # pragma: no cover - optional on older Qt releases
    from PySide6.QtGui import QColorTransform
except ImportError:  # pragma: no cover - Qt < 6.4 lacks QColorTransform
    QColorTransform = None  # type: ignore[assignment]
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat, QVideoSink


class HdrVideoFrameProcessor(QObject):
    """Tone map HDR frames emitted by a :class:`QVideoSink` into SDR space.

    Qt's multimedia stack passes HDR metadata through the video pipeline but
    relies on the platform compositor to apply the actual tone mapping step.
    On systems that lack HDR output support this results in washed-out footage
    because PQ or HLG encoded frames are simply interpreted as if they were
    already SDR.  The processor listens for incoming frames and, whenever the
    metadata advertises BT.2020 primaries with an HDR transfer function, it
    converts the image into standard sRGB using Qt's colour management APIs.

    The work happens entirely on the CPU via :class:`QImage`, which keeps the
    code path platform neutral while guaranteeing deterministic output.  SDR
    footage is left untouched so there is no performance penalty when the
    source already targets standard dynamic range displays.
    """

    def __init__(self, sink: QVideoSink, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._sink = sink
        self._processing = False
        self._target_space = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
        self._sink.videoFrameChanged.connect(self._handle_frame)

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------
    def _handle_frame(self, frame: QVideoFrame) -> None:
        """Apply tone mapping when *frame* carries HDR metadata."""

        if self._processing:
            # ``setVideoFrame`` below triggers ``videoFrameChanged`` again.  Use
            # a guard to avoid recursively re-processing the already converted
            # frame.
            return

        processed = self._tone_map_frame(frame)
        if processed is None:
            return

        self._processing = True
        try:
            self._sink.setVideoFrame(processed)
        finally:
            self._processing = False

    def _tone_map_frame(self, frame: QVideoFrame) -> Optional[QVideoFrame]:
        """Return a tone-mapped version of *frame* or ``None`` for SDR input."""

        if not frame.isValid():
            return None

        surface_format = frame.surfaceFormat()
        if not surface_format.isValid():
            return None
        if not self._is_hdr_format(surface_format):
            return None

        image = frame.toImage()
        if image.isNull():
            return None

        self._ensure_source_color_space(image, surface_format)
        mapped = self._convert_to_sdr(image)
        if mapped is None or mapped.isNull():
            return None

        return QVideoFrame(mapped)

    # ------------------------------------------------------------------
    # Colour helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_hdr_format(surface_format: QVideoFrameFormat) -> bool:
        """Return ``True`` when *surface_format* advertises HDR metadata."""

        if surface_format.colorSpace() != QVideoFrameFormat.ColorSpace.ColorSpace_BT2020:
            return False
        transfer = surface_format.colorTransfer()
        return transfer in {
            QVideoFrameFormat.ColorTransfer.ColorTransfer_ST2084,
            QVideoFrameFormat.ColorTransfer.ColorTransfer_STD_B67,
        }

    def _ensure_source_color_space(
        self, image: QImage, surface_format: QVideoFrameFormat
    ) -> None:
        """Assign an accurate colour space to *image* when metadata is present."""

        if image.colorSpace().isValid():
            return

        named_space = self._named_color_space(surface_format)
        if named_space is None:
            return

        try:
            image.setColorSpace(QColorSpace(named_space))
        except Exception:
            # Fallback silently when Qt cannot apply the colour space; the
            # subsequent conversion step will still enforce sRGB output.
            return

    def _convert_to_sdr(self, image: QImage) -> Optional[QImage]:
        """Convert *image* to the configured SDR target space."""

        source_space = image.colorSpace()
        if not source_space.isValid():
            # Without a valid source profile the conversion becomes ambiguous.
            return None

        working = image
        if working.format() not in {
            QImage.Format.Format_RGBA64,
            QImage.Format.Format_RGBA16,
            QImage.Format.Format_RGBA8888,
            QImage.Format.Format_RGBA8888_Premultiplied,
            QImage.Format.Format_ARGB32,
            QImage.Format.Format_ARGB32_Premultiplied,
        }:
            try:
                working = image.convertToFormat(QImage.Format.Format_RGBA64)
            except Exception:
                working = QImage(image)

        converted: Optional[QImage] = None

        transform = None
        if QColorTransform is not None:
            try:
                transform = QColorTransform.fromColorSpaces(source_space, self._target_space)
            except Exception:
                transform = None

        if transform is not None:
            try:
                converted = transform.map(working)
            except Exception:
                converted = None

        if converted is None or converted.isNull():
            try:
                converted = working.convertedToColorSpace(self._target_space)
            except Exception:
                converted = None

        if converted is None or converted.isNull():
            return None

        if not converted.colorSpace().isValid():
            try:
                converted.setColorSpace(self._target_space)
            except Exception:
                pass

        if converted.format() not in {
            QImage.Format.Format_ARGB32,
            QImage.Format.Format_ARGB32_Premultiplied,
        }:
            converted = converted.convertToFormat(QImage.Format.Format_ARGB32)

        return converted

    @staticmethod
    def _named_color_space(
        surface_format: QVideoFrameFormat,
    ) -> Optional[QColorSpace.NamedColorSpace]:
        """Map video metadata to a :class:`QColorSpace.NamedColorSpace`."""

        if surface_format.colorSpace() != QVideoFrameFormat.ColorSpace.ColorSpace_BT2020:
            return None

        transfer = surface_format.colorTransfer()
        if transfer == QVideoFrameFormat.ColorTransfer.ColorTransfer_ST2084:
            return QColorSpace.NamedColorSpace.Bt2100Pq
        if transfer == QVideoFrameFormat.ColorTransfer.ColorTransfer_STD_B67:
            return QColorSpace.NamedColorSpace.Bt2100Hlg
        return QColorSpace.NamedColorSpace.Bt2020

