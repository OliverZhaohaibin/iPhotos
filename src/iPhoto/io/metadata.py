"""Metadata readers for still images and video clips."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dateutil.parser import isoparse
from dateutil.tz import gettz

from ..errors import ExternalToolError
from ..utils.deps import load_pillow
from ..utils.exiftool import get_metadata as get_exiftool_metadata
from ..utils.ffmpeg import probe_media

_PILLOW = load_pillow()

if _PILLOW is not None:
    Image = _PILLOW.Image
    UnidentifiedImageError = _PILLOW.UnidentifiedImageError
else:  # pragma: no cover - exercised only when Pillow is missing
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = None  # type: ignore[assignment]


def _empty_image_info() -> Dict[str, Any]:
    """Return a metadata stub used whenever inspection fails."""

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
    """Return an ISO-8601 UTC timestamp for a naive EXIF ``DateTime`` value."""

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
        if len(offset) == 5 and offset[0] in "+-":
            offset = f"{offset[:3]}:{offset[3:]}"
        combined = f"{dt_value}{offset}"
        try:
            captured = datetime.strptime(combined, f"{fmt}%z")
            return _format_result(captured)
        except ValueError:
            pass

    try:
        naive = datetime.strptime(dt_value, fmt)
    except ValueError:
        return None

    local_tz = gettz() or datetime.now().astimezone().tzinfo or timezone.utc
    return _format_result(naive.replace(tzinfo=local_tz))


def _parse_dms(value: str) -> Optional[float]:
    """Convert a degrees/minutes/seconds string into decimal degrees."""

    cleaned = value.strip()
    if not cleaned:
        return None

    # Extract the hemisphere marker so we can maintain the sign correctly.
    direction = 1.0
    if cleaned[-1] in "NSEWnsew":
        last = cleaned[-1].upper()
        cleaned = cleaned[:-1].strip()
        if last in {"S", "W"}:
            direction = -1.0

    # Replace textual markers with spaces so the components can be parsed safely.
    normalised = re.sub(r"[^0-9\.]+", " ", cleaned)
    parts = [segment for segment in normalised.split() if segment]
    if not parts:
        return None

    try:
        degrees = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
    except ValueError:
        return None

    return direction * (degrees + minutes / 60.0 + seconds / 3600.0)


def _coerce_decimal(value: Any) -> Optional[float]:
    """Return ``value`` as a floating point number when possible."""

    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            return float(candidate)
        except ValueError:
            return _parse_dms(candidate)
    return None


def _extract_gps_from_exiftool(meta: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Extract decimal GPS coordinates from an ExifTool metadata payload."""

    key_pairs = [
        ("Composite:GPSLatitude", "Composite:GPSLongitude"),
        ("EXIF:GPSLatitude", "EXIF:GPSLongitude"),
        ("XMP:GPSLatitude", "XMP:GPSLongitude"),
    ]
    for lat_key, lon_key in key_pairs:
        latitude = _coerce_decimal(meta.get(lat_key))
        longitude = _coerce_decimal(meta.get(lon_key))
        if latitude is not None and longitude is not None:
            return {"lat": latitude, "lon": longitude}

    composite_position = meta.get("Composite:GPSPosition")
    if isinstance(composite_position, str):
        tokens = [
            segment
            for segment in composite_position.replace(",", " ").split()
            if segment
        ]
        if len(tokens) >= 2:
            latitude = _coerce_decimal(tokens[0])
            longitude = _coerce_decimal(tokens[1])
            if latitude is not None and longitude is not None:
                return {"lat": latitude, "lon": longitude}

    return None


def _extract_datetime_from_exiftool(meta: Dict[str, Any]) -> Optional[str]:
    """Extract a UTC ISO-8601 timestamp from an ExifTool metadata payload."""

    candidate_keys = [
        "EXIF:DateTimeOriginal",
        "EXIF:CreateDate",
        "EXIF:ModifyDate",
        "QuickTime:CreateDate",
        "QuickTime:ModifyDate",
    ]
    for key in candidate_keys:
        value = meta.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = isoparse(value)
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is None:
            local_tz = gettz() or datetime.now().astimezone().tzinfo or timezone.utc
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def read_image_meta(path: Path) -> Dict[str, Any]:
    """Read metadata for ``path`` using Pillow and ExifTool."""

    print(f"Opening image for metadata: {path}")
    info = _empty_image_info()

    # Pillow is used for fast dimension lookups.  Keep its usage optional so the
    # reader still works on systems where Pillow is not installed.
    exif_payload: Any = None
    if Image is not None and UnidentifiedImageError is not None:
        try:
            with Image.open(path) as img:
                info["w"] = img.width
                info["h"] = img.height
                info["mime"] = Image.MIME.get(img.format, None)
                exif_payload = img.getexif() if hasattr(img, "getexif") else None
        except UnidentifiedImageError as exc:
            raise ExternalToolError(f"Unable to read image metadata for {path}") from exc
        except OSError:
            # Pillow may raise ``OSError`` for truncated or unsupported files.
            pass

    gps_found = False
    try:
        metadata_block = get_exiftool_metadata(path)
    except ExternalToolError as exc:
        print(f"Warning: Could not use ExifTool for {path}: {exc}")
        metadata_block = None

    if metadata_block:
        # Diagnostic print to surface the entire ExifTool payload so we can see
        # which keys are populated on the user's system.
        print(f"\n--- METADATA FOR {path.name} ---")
        print(json.dumps(metadata_block, indent=2, ensure_ascii=False))
        print("--- END METADATA ---\n")

        gps_payload = _extract_gps_from_exiftool(metadata_block)
        if gps_payload is not None:
            info["gps"] = gps_payload
            gps_found = True
        dt_value = _extract_datetime_from_exiftool(metadata_block)
        if dt_value:
            info["dt"] = dt_value

    if info["dt"] is None and exif_payload:
        fallback_dt = exif_payload.get(36867) or exif_payload.get(306)
        if isinstance(fallback_dt, str):
            info["dt"] = _normalise_exif_datetime(fallback_dt, exif_payload)

    if gps_found:
        print(
            "Extracted GPS coordinates via ExifTool: "
            f"lat={info['gps']['lat']:.6f}, lon={info['gps']['lon']:.6f}"
        )
    else:
        print("No GPS coordinates found in metadata")

    return info


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


__all__ = ["read_image_meta", "read_video_meta"]
