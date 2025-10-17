"""Lightweight wrappers around the ``ffmpeg`` toolchain."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..errors import ExternalToolError

try:  # pragma: no cover - optional dependency detection
    import cv2  # type: ignore
except Exception:  # pragma: no cover - OpenCV not available or broken
    cv2 = None  # type: ignore[assignment]

_FFMPEG_LOG_LEVEL = "error"


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

    try:
        metadata = probe_media(source)
    except ExternalToolError:
        # When ffprobe is unavailable we silently fall back to SDR processing,
        # ensuring callers still get a frame even though colour accuracy may be
        # reduced for true HDR sources.
        return False

    streams = metadata.get("streams")
    if not isinstance(streams, list):
        return False

    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue

        # HDR videos typically expose either the BT.2020 colour primaries or an
        # HDR transfer curve such as PQ (SMPTE ST 2084) or HLG
        # (ARIB STD-B67).  Detecting either signal is sufficient to warrant
        # tone mapping.
        color_transfer = stream.get("color_transfer")
        color_space = stream.get("color_space")
        if color_transfer in {"smpte2084", "arib-std-b67"}:
            return True
        if color_space in {"bt2020", "bt2020nc"}:
            return True

    return False


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
    if is_hdr_video(source):
        # HDR-only processing path: the filter chain linearises the image,
        # performs tone mapping into the BT.709 gamut, and re-encodes the signal
        # for standard dynamic range output.  SDR sources bypass this expensive
        # conversion to preserve their original colours.
        filters.extend(
            [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "zscale=p=bt709",
                "tonemap=tonemap=hable:desat=0",
                "zscale=t=bt709:m=bt709:r=tv",
            ]
        )

    if scale is not None:
        width, height = scale
        if width > 0 and height > 0:
            scale_filter = "scale=min({w},iw):min({h},ih):force_original_aspect_ratio=decrease".format(
                w=width,
                h=height,
            )
            if filters:
                filters.append(scale_filter)
            else:
                filters.insert(0, scale_filter)

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
