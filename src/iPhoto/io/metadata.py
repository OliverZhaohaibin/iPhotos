"""Metadata readers for media assets."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from PIL import Image, UnidentifiedImageError

try:  # pragma: no cover - optional dependency registration
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover
    register_heif_opener = None

from ..errors import ExternalToolError
from ..utils.ffmpeg import probe_media

if register_heif_opener is not None:  # pragma: no branch
    register_heif_opener()


def read_image_meta(path: Path) -> Dict[str, Any]:
    """Read metadata for an image file using Pillow."""

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
