"""Metadata readers for media assets."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from dateutil.tz import gettz

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


def _normalise_exif_datetime(dt_value: str, exif: Any) -> Optional[str]:
    """Return an ISO8601 UTC timestamp for an EXIF ``DateTime`` value.

    Many cameras record ``DateTimeOriginal`` without a timezone. When the
    companion ``OffsetTime`` tags are available we combine them. Otherwise we
    treat the naive timestamp as local time and convert to UTC so that
    subsequent pairing logic can compare still and motion captures reliably.
    """

    fmt = "%Y:%m:%d %H:%M:%S"
    offset_tags = (36880, 36881, 36882)
    offset: Optional[str] = None
    for tag in offset_tags:
        value = exif.get(tag)
        if isinstance(value, str) and value.strip():
            offset = value.strip()
            break

    def _format_result(captured: datetime) -> str:
        return captured.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    if offset:
        # Normalise offsets like ``+0800`` to ``+08:00`` because Pillow may
        # preserve either representation depending on the source.
        if len(offset) == 5 and offset[0] in "+-":
            offset = f"{offset[:3]}:{offset[3:]}"
        combined = f"{dt_value}{offset}"
        try:
            captured = datetime.strptime(combined, f"{fmt}%z")
            return _format_result(captured)
        except ValueError:
            # Fall back to interpreting it as local time when the offset is
            # malformed. This mirrors the behaviour used when the offset tag
            # is missing entirely.
            pass

    try:
        naive = datetime.strptime(dt_value, fmt)
    except ValueError:
        return None

    # ``dateutil.tz.gettz`` honours daylight saving transitions for the
    # current locale, making the behaviour both predictable for callers and
    # easy to override in tests.
    local_tz = gettz() or datetime.now().astimezone().tzinfo or timezone.utc
    return _format_result(naive.replace(tzinfo=local_tz))


def _rational_to_float(value: Any) -> Optional[float]:
    """Normalise EXIF rational values to plain floats."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Sequence) and len(value) == 2:
        numerator, denominator = value
        try:
            numerator = float(numerator)
            denominator = float(denominator)
        except (TypeError, ValueError):
            return None
        if denominator == 0:
            return None
        return numerator / denominator
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    if numerator is None or denominator in {None, 0}:
        return None
    try:
        return float(numerator) / float(denominator)
    except (TypeError, ValueError):
        return None


def _dms_to_degrees(values: Sequence[Any], ref: Any) -> Optional[float]:
    """Convert EXIF degrees/minutes/seconds tuples to decimal degrees."""

    if not values or len(values) < 3:
        return None
    parts = []
    for item in values[:3]:
        number = _rational_to_float(item)
        if number is None:
            return None
        parts.append(number)
    degrees = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", errors="ignore")
    if isinstance(ref, str):
        ref = ref.strip().upper()
        if ref in {"S", "W"}:
            degrees = -degrees
    return degrees


def _extract_gps_coordinates(exif: Any) -> Optional[Dict[str, float]]:
    """Parse GPS latitude/longitude from a Pillow EXIF payload."""

    gps_tag = exif.get(34853)
    if not isinstance(gps_tag, dict):
        return None

    lat_values = gps_tag.get(2)
    lat_ref = gps_tag.get(1)
    lon_values = gps_tag.get(4)
    lon_ref = gps_tag.get(3)

    latitude = _dms_to_degrees(lat_values, lat_ref)
    longitude = _dms_to_degrees(lon_values, lon_ref)
    if latitude is None or longitude is None:
        return None
    return {"lat": latitude, "lon": longitude}


def read_image_meta(path: Path) -> Dict[str, Any]:
    """Read metadata for an image file using Pillow."""

    if Image is None or UnidentifiedImageError is None:
        return _empty_image_info()

    try:
        print(f"Opening image for metadata inspection: {path}")
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
                    info["dt"] = _normalise_exif_datetime(dt_value, exif)
                gps_info = _extract_gps_coordinates(exif)
                if gps_info is not None:
                    # Emit a debug line so users can observe captured GPS coordinates.
                    print(f"Extracted GPS coordinates: {gps_info}")
                    info["gps"] = gps_info
                else:
                    # Provide feedback when no coordinates are present in the EXIF payload.
                    print("No GPS coordinates found in image metadata.")
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
