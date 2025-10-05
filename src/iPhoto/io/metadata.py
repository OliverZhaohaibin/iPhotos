"""Metadata readers for media assets."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from ..errors import ExternalToolError
from ..utils.ffmpeg import probe_media
from ..utils.deps import load_pillow

_PILLOW = load_pillow()

if _PILLOW is not None:
    Image = _PILLOW.Image
    UnidentifiedImageError = _PILLOW.UnidentifiedImageError
else:  # pragma: no cover - exercised only when Pillow is missing
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = None  # type: ignore[assignment]

def _empty_image_info() -> Dict[str, Any]:
    """Return a metadata stub when image inspection fails."""

    return {
        "w": None,
        "h": None,
        "mime": None,
        "dt": None,
        "make": None,
        "model": None,
        "gps": None,
        "content_id": None,
    }


def read_image_meta(path: Path) -> Dict[str, Any]:
    """Read metadata for an image file using Pillow."""

    if Image is None or UnidentifiedImageError is None:
        return _empty_image_info()

    try:
        with Image.open(path) as img:
            exif = img.getexif() if hasattr(img, "getexif") else None
            info: Dict[str, Any] = {
                "w": img.width,
                "h": img.height,
                "mime": Image.MIME.get(img.format, None),
                "dt": None,
                "make": None,
                "model": None,
                "gps": None,
                "content_id": None,
            }
            if exif:
                dt_value = exif.get(36867) or exif.get(306)
                if isinstance(dt_value, str):
                    try:
                        captured = datetime.strptime(dt_value, "%Y:%m:%d %H:%M:%S")
                        info["dt"] = captured.replace(tzinfo=timezone.utc).isoformat().replace(
                            "+00:00", "Z"
                        )
                    except ValueError:
                        info["dt"] = None
            return info
    except UnidentifiedImageError as exc:
        raise ExternalToolError(f"Unable to read image metadata for {path}") from exc
    except OSError:
        # ``Image.open`` may raise ``OSError`` for minimal or truncated images such
        # as the 1x1 PNG fixtures used in sidebar tests. Treat those as missing
        # metadata instead of aborting the scan so the index can still be built.
        return _empty_image_info()


def read_video_meta(path: Path) -> Dict[str, Any]:
    """Return basic metadata for a video file."""

    mime = "video/quicktime" if path.suffix.lower() in {".mov", ".qt"} else "video/mp4"
    info: Dict[str, Any] = {
        "mime": mime,
        "dur": None,
        "codec": None,
        "content_id": None,
        "still_image_time": None,
        "w": None,
        "h": None,
    }
    try:
        metadata = probe_media(path)
    except ExternalToolError:
        return info

    fmt = metadata.get("format", {}) if isinstance(metadata, dict) else {}
    duration = fmt.get("duration")
    if isinstance(duration, str):
        try:
            info["dur"] = float(duration)
        except ValueError:
            info["dur"] = None
    streams = metadata.get("streams", []) if isinstance(metadata, dict) else []
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if stream.get("codec_type") == "video":
                codec = stream.get("codec_name")
                if isinstance(codec, str):
                    info["codec"] = codec
                width = stream.get("width")
                height = stream.get("height")
                if isinstance(width, int) and isinstance(height, int):
                    info["w"] = width
                    info["h"] = height
                tags = stream.get("tags")
                if isinstance(tags, dict):
                    still_time = tags.get("com.apple.quicktime.still-image-time")
                    if isinstance(still_time, str):
                        try:
                            info["still_image_time"] = float(still_time)
                        except ValueError:
                            info["still_image_time"] = None
            elif stream.get("codec_type") == "audio":
                codec = stream.get("codec_name")
                if isinstance(codec, str) and not info.get("codec"):
                    info["codec"] = codec
    return info
