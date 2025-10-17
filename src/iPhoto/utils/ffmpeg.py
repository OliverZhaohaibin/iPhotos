"""Lightweight wrappers around the ``ffmpeg`` toolchain."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..errors import ExternalToolError

try:  # pragma: no cover - optional dependency detection
    import cv2  # type: ignore
except Exception:  # pragma: no cover - OpenCV not available or broken
    cv2 = None  # type: ignore[assignment]

_FFMPEG_LOG_LEVEL = "error"

_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_HDR_PRIMARIES = {"bt2020", "smpte431", "smpte432"}
_HDR_MATRICES = {"bt2020ncl", "bt2020cl"}
_HDR_SIDE_DATA = {
    "Mastering display metadata",
    "Content light level metadata",
    "HDR Dynamic Metadata (HDR10+)",
    "HDR10+ Dynamic Metadata",
}


@dataclass(frozen=True)
class VideoColorProfile:
    """Snapshot of the colour configuration advertised by a video stream."""

    primaries: Optional[str]
    transfer: Optional[str]
    matrix: Optional[str]
    range: Optional[str]
    peak_luminance: Optional[float]
    has_hdr_metadata: bool

    @property
    def is_hdr(self) -> bool:
        """Return ``True`` when the stream exposes HDR-specific metadata."""

        return self.has_hdr_metadata


def _normalise_primaries(value: Optional[str]) -> Optional[str]:
    """Translate ffprobe colour primaries into names accepted by ``zscale``."""

    if not value:
        return None
    mapping = {
        "bt709": "bt709",
        "bt470bg": "bt470bg",
        "smpte170m": "smpte170m",
        "smpte240m": "smpte240m",
        "film": "film",
        "smpte431": "smpte431",
        "smpte432": "smpte432",
        "bt2020": "bt2020",
    }
    return mapping.get(value.lower())


def _normalise_transfer(value: Optional[str]) -> Optional[str]:
    """Translate ffprobe transfer characteristics into ``zscale`` names."""

    if not value:
        return None
    mapping = {
        "bt709": "bt709",
        "gamma22": "bt709",
        "gamma28": "gamma28",
        "smpte170m": "smpte170m",
        "smpte240m": "smpte240m",
        "linear": "linear",
        "log": "log",
        "log-sqrt": "log_sqrt",
        "iec61966-2-4": "iec61966-2-4",
        "iec61966-2-1": "iec61966-2-1",
        "bt1361": "bt1361",
        "smpte2084": "smpte2084",
        "arib-std-b67": "arib-std-b67",
    }
    return mapping.get(value.lower())


def _normalise_matrix(value: Optional[str]) -> Optional[str]:
    """Translate ffprobe matrix coefficients into ``zscale`` names."""

    if not value:
        return None
    mapping = {
        "bt709": "bt709",
        "fcc": "fcc",
        "smpte170m": "smpte170m",
        "bt470bg": "bt470bg",
        "smpte240m": "smpte240m",
        "ycgco": "ycgco",
        "bt2020nc": "bt2020ncl",
        "bt2020c": "bt2020cl",
        "rgb": "rgb",
    }
    return mapping.get(value.lower())


def _normalise_range(value: Optional[str]) -> Optional[str]:
    """Return the value when ffprobe exposes a supported colour range name."""

    if not value:
        return None
    lower = value.lower()
    if lower in {"tv", "pc", "limited", "full"}:
        # ``zscale`` understands ``tv``/``pc`` and the synonyms ``limited``/``full``.
        if lower == "limited":
            return "tv"
        if lower == "full":
            return "pc"
        return lower
    return None


def _parse_fraction(value: Any) -> Optional[float]:
    """Parse ``value`` into a float, accepting ffprobe's rational strings."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            try:
                num = float(numerator)
                denom = float(denominator)
            except ValueError:
                return None
            if denom == 0:
                return None
            return num / denom
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _video_color_profile(source: Path) -> VideoColorProfile:
    """Return the advertised colour metadata for the first video stream."""

    try:
        metadata = probe_media(source)
    except ExternalToolError:
        # Without ffprobe we have no reliable metadata.  Assume SDR so we do not
        # perform unnecessary tone mapping while still returning a valid profile.
        return VideoColorProfile(None, None, None, None, None, False)

    streams = metadata.get("streams")
    stream_data: Optional[Dict[str, Any]] = None
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                stream_data = stream
                break

    if stream_data is None:
        return VideoColorProfile(None, None, None, None, None, False)

    primaries = _normalise_primaries(stream_data.get("color_primaries"))
    transfer = _normalise_transfer(stream_data.get("color_transfer"))
    matrix = _normalise_matrix(stream_data.get("color_space"))
    colour_range = _normalise_range(stream_data.get("color_range"))

    raw_primaries = str(stream_data.get("color_primaries") or "").lower()
    raw_transfer = str(stream_data.get("color_transfer") or "").lower()
    raw_matrix = str(stream_data.get("color_space") or "").lower()

    has_hdr = False
    if transfer in _HDR_TRANSFERS or raw_transfer in _HDR_TRANSFERS:
        has_hdr = True
    elif primaries in _HDR_PRIMARIES or raw_primaries in _HDR_PRIMARIES:
        has_hdr = True
    elif matrix in _HDR_MATRICES or raw_matrix in _HDR_MATRICES:
        has_hdr = True

    peak_luminance: Optional[float] = None
    side_data = stream_data.get("side_data_list")
    if isinstance(side_data, list):
        for entry in side_data:
            if not isinstance(entry, dict):
                continue
            side_type = entry.get("side_data_type")
            if isinstance(side_type, str):
                lower_type = side_type.lower()
                if (
                    side_type in _HDR_SIDE_DATA
                    or "hdr" in lower_type
                    or "dolby vision" in lower_type
                ):
                    has_hdr = True
                if side_type == "Mastering display metadata":
                    parsed = _parse_fraction(entry.get("max_luminance"))
                    if parsed is not None:
                        peak_luminance = max(parsed, peak_luminance or 0.0)
                elif side_type == "Content light level metadata":
                    parsed = _parse_fraction(entry.get("max_content"))
                    if parsed is not None:
                        peak_luminance = max(parsed, peak_luminance or 0.0)

    if peak_luminance is None and has_hdr:
        # HDR10 content commonly masters at 1000 nits, so that serves as a
        # sensible default when the container omits explicit metadata.
        peak_luminance = 1000.0

    if matrix is None:
        if primaries == "bt2020":
            matrix = "bt2020ncl"
        elif primaries in {"smpte431", "smpte432"}:
            matrix = "rgb"
        else:
            matrix = "bt709"

    if transfer is None and has_hdr:
        # Prefer PQ as a fallback because most HDR10 clips encode with SMPTE ST 2084.
        transfer = "smpte2084"

    if colour_range is None and has_hdr:
        colour_range = "tv"

    return VideoColorProfile(primaries, transfer, matrix, colour_range, peak_luminance, has_hdr)


def _build_zscale_filter(options: Sequence[tuple[str, Optional[str]]]) -> str:
    """Return a ``zscale`` filter description excluding ``None`` assignments."""

    assignments = [f"{key}={value}" for key, value in options if value is not None]
    if not assignments:
        return "zscale"
    return "zscale=" + ":".join(assignments)


def _build_hdr_tonemap_filters(profile: VideoColorProfile) -> list[str]:
    """Construct a filter chain that tone maps HDR frames into SDR."""

    primaries = profile.primaries or "bt2020"
    matrix = profile.matrix or ("bt2020ncl" if primaries == "bt2020" else "bt709")
    transfer = profile.transfer or "smpte2084"
    colour_range = profile.range or "tv"

    pre_tonemap = _build_zscale_filter(
        [
            ("pin", primaries),
            ("tin", transfer),
            ("min", matrix),
            ("rin", colour_range),
            ("p", primaries),
            ("t", "linear"),
            ("m", matrix),
            ("r", colour_range),
            ("npl", "100"),
        ]
    )

    tonemap_parts = ["tonemap=tonemap=hable", "desat=0"]
    if profile.peak_luminance is not None:
        tonemap_parts.append(f"peak={profile.peak_luminance:.4g}")
    tonemap_filter = ":".join(tonemap_parts)

    post_tonemap = _build_zscale_filter(
        [
            ("p", "bt709"),
            ("t", "bt709"),
            ("m", "bt709"),
            ("r", "tv"),
            ("dither", "none"),
        ]
    )

    return [pre_tonemap, "format=gbrpf32le", tonemap_filter, post_tonemap]


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Execute *command* and return the completed process."""

    try:
        process = subprocess.run(
            list(command),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # pragma: no cover - depends on environment
        raise ExternalToolError("ffmpeg executable not found on PATH") from exc
    return process


def is_hdr_video(source: Path) -> bool:
    """Return ``True`` when *source* is tagged with HDR transfer characteristics.

    The helper inspects ``ffprobe`` metadata because SDR and HDR assets require
    different handling when extracting representative thumbnails.  Applying tone
    mapping to every video would wash out SDR footage, so we only do the
    expensive conversion when the stream advertises modern HDR colour primaries
    or transfer functions (for example PQ or HLG).
    """

    return _video_color_profile(source).is_hdr


def extract_video_frame(
    source: Path,
    *,
    at: Optional[float] = None,
    scale: Optional[tuple[int, int]] = None,
    format: str = "jpeg",
) -> bytes:
    """Return a still frame extracted from *source*.

    Parameters
    ----------
    source:
        Path to the input video file.
    at:
        Timestamp in seconds to sample. When ``None`` the first frame is used.
    scale:
        Optional ``(width, height)`` hint used to scale the output frame while
        preserving aspect ratio.
    format:
        Output image format. ``"jpeg"`` is used by default because Qt decoders
        handle it more reliably on Windows. ``"png"`` remains available for
        callers that prefer lossless output.
    """

    fmt = format.lower()
    if fmt not in {"png", "jpeg"}:
        raise ValueError("format must be either 'png' or 'jpeg'")

    try:
        return _extract_with_ffmpeg(source, at=at, scale=scale, format=fmt)
    except ExternalToolError as exc:
        fallback = _extract_with_opencv(source, at=at, scale=scale, format=fmt)
        if fallback is not None:
            return fallback
        raise exc


def _extract_with_ffmpeg(
    source: Path,
    *,
    at: Optional[float],
    scale: Optional[tuple[int, int]],
    format: str,
) -> bytes:
    profile = _video_color_profile(source)
    suffix = ".png" if format == "png" else ".jpg"
    codec = "png" if format == "png" else "mjpeg"

    command: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        _FFMPEG_LOG_LEVEL,
        "-nostdin",
        "-y",
    ]
    if at is not None:
        command += ["-ss", f"{max(at, 0):.3f}"]
    command += [
        "-i",
        str(source),
        "-an",
        "-frames:v",
        "1",
        "-vsync",
        "0",
    ]
    filters: list[str] = []
    if profile.is_hdr:
        filters.extend(_build_hdr_tonemap_filters(profile))

    if scale is not None:
        width, height = scale
        if width > 0 and height > 0:
            scale_filter = "scale=min({w},iw):min({h},ih):force_original_aspect_ratio=decrease".format(
                w=width,
                h=height,
            )
            filters.append(scale_filter)

    if format == "jpeg":
        # JPEG encoders expect even dimensions and typically operate on YUV data. The
        # extra scaling step enforces an even size while keeping aspect ratio untouched.
        filters.append("scale=max(2,trunc(iw/2)*2):max(2,trunc(ih/2)*2)")
        filters.append("format=yuv420p")
    elif format == "png":
        # Preserve the alpha channel for PNG outputs to avoid losing transparency.
        filters.append("format=rgba")
    else:
        filters.append("format=yuv420p")
    if filters:
        command += ["-vf", ",".join(filters)]
    command += ["-f", "image2", "-vcodec", codec]
    if format == "jpeg":
        command += ["-q:v", "2"]

    fd, tmp_name = tempfile.mkstemp(suffix=suffix)
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        command.append(str(tmp_path))
        process = _run_command(command)
        if process.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            stderr = process.stderr.decode("utf-8", "ignore").strip()
            raise ExternalToolError(
                f"ffmpeg failed to extract frame from {source}: {stderr or 'unknown error'}"
            )
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


def _extract_with_opencv(
    source: Path,
    *,
    at: Optional[float],
    scale: Optional[tuple[int, int]],
    format: str,
) -> Optional[bytes]:
    if cv2 is None:
        return None

    try:
        capture = cv2.VideoCapture(str(source))
    except Exception:
        return None

    is_opened = True
    try:
        is_opened = bool(capture.isOpened())
    except Exception:
        is_opened = False
    if not is_opened:
        try:
            capture.release()
        except Exception:
            pass
        return None

    try:
        if at is not None and at >= 0:
            seconds = max(at, 0.0)
            try:
                positioned = capture.set(getattr(cv2, "CAP_PROP_POS_MSEC", 0), seconds * 1000.0)
            except Exception:
                positioned = False
            if not positioned:
                try:
                    fps = capture.get(getattr(cv2, "CAP_PROP_FPS", 5.0))
                except Exception:
                    fps = 0.0
                if fps and fps > 0:
                    try:
                        capture.set(
                            getattr(cv2, "CAP_PROP_POS_FRAMES", 1),
                            max(int(round(fps * seconds)), 0),
                        )
                    except Exception:
                        pass
        ok, frame = capture.read()
    except Exception:
        return None
    finally:
        try:
            capture.release()
        except Exception:
            pass

    if not ok or frame is None:
        return None

    try:
        height, width = frame.shape[:2]
    except Exception:
        return None

    target_frame = frame
    if (
        scale is not None
        and width > 0
        and height > 0
        and scale[0] > 0
        and scale[1] > 0
    ):
        max_width, max_height = scale
        ratio = min(max_width / width, max_height / height)
        if ratio < 1.0:
            new_width = max(int(width * ratio), 1)
            new_height = max(int(height * ratio), 1)
            if format == "jpeg":
                if new_width % 2 == 1 and new_width > 1:
                    new_width -= 1
                if new_height % 2 == 1 and new_height > 1:
                    new_height -= 1
            interpolation = getattr(cv2, "INTER_AREA", 3)
            try:
                target_frame = cv2.resize(target_frame, (new_width, new_height), interpolation=interpolation)
            except Exception:
                return None

    extension = ".png" if format == "png" else ".jpg"
    params: list[int] = []
    if format == "jpeg":
        jpeg_quality = getattr(cv2, "IMWRITE_JPEG_QUALITY", None)
        if jpeg_quality is not None:
            params = [int(jpeg_quality), 92]

    try:
        success, buffer = cv2.imencode(extension, target_frame, params)
    except Exception:
        return None

    if not success:
        return None

    try:
        return bytes(buffer)
    except Exception:
        return None

def probe_media(source: Path) -> Dict[str, Any]:
    """Return ffprobe metadata for *source*.

    The JSON structure mirrors ffprobe's ``show_format`` and ``show_streams``
    output. ``ExternalToolError`` is raised when the toolchain is unavailable or
    returns an error.
    """

    command = [
        "ffprobe",
        "-hide_banner",
        "-loglevel",
        _FFMPEG_LOG_LEVEL,
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]

    process = _run_command(command)
    if process.returncode != 0 or not process.stdout:
        stderr = process.stderr.decode("utf-8", "ignore").strip()
        raise ExternalToolError(
            f"ffprobe failed to inspect {source}: {stderr or 'unknown error'}"
        )
    try:
        return json.loads(process.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ExternalToolError("ffprobe returned invalid JSON output") from exc
