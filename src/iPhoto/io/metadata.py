"""Metadata readers for still images and video clips."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dateutil.parser import isoparse
from dateutil.tz import gettz

from ..errors import ExternalToolError
from ..utils.deps import load_pillow
from ..utils.exiftool import get_metadata_batch
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
    """Normalise an EXIF ``DateTime`` string to a UTC ISO-8601 representation."""

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
            return None
    return None


def _extract_group(metadata: Dict[str, Any], group_name: str) -> Optional[Dict[str, Any]]:
    """Return an ExifTool group mapping from either nested or flattened layouts."""

    group = metadata.get(group_name)
    if isinstance(group, dict):
        return group

    # When ``-g1`` is combined with ``-json`` the output may already be nested.
    # Older configurations without that flag expose keys such as ``Composite:Foo``.
    prefix = f"{group_name}:"
    extracted = {
        key[len(prefix) :]: value
        for key, value in metadata.items()
        if isinstance(key, str) and key.startswith(prefix)
    }
    return extracted or None


def _extract_gps_from_exiftool(meta: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Extract decimal GPS coordinates from ExifTool's metadata payload."""

    composite = _extract_group(meta, "Composite")
    if composite:
        lat = _coerce_decimal(composite.get("GPSLatitude"))
        lon = _coerce_decimal(composite.get("GPSLongitude"))
        if lat is not None and lon is not None:
            return {"lat": lat, "lon": lon}

    gps_group = _extract_group(meta, "GPS")
    if gps_group:
        lat = _coerce_decimal(gps_group.get("GPSLatitude"))
        lon = _coerce_decimal(gps_group.get("GPSLongitude"))
        if lat is not None and lon is not None:
            lat_ref = str(gps_group.get("GPSLatitudeRef", "N")).upper()
            lon_ref = str(gps_group.get("GPSLongitudeRef", "E")).upper()
            if lat_ref == "S":
                lat = -lat
            if lon_ref == "W":
                lon = -lon
            return {"lat": lat, "lon": lon}

    return None


def _extract_datetime_from_exiftool(meta: Dict[str, Any]) -> Optional[str]:
    """Extract a UTC ISO-8601 timestamp from the ExifTool metadata payload."""

    composite = _extract_group(meta, "Composite")
    if composite:
        for key in ("SubSecDateTimeOriginal", "SubSecCreateDate", "GPSDateTime"):
            value = composite.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    parsed = isoparse(value)
                except (ValueError, TypeError):
                    continue
                if parsed.tzinfo is None:
                    local_tz = gettz() or datetime.now().astimezone().tzinfo or timezone.utc
                    parsed = parsed.replace(tzinfo=local_tz)
                return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    exif_ifd = _extract_group(meta, "ExifIFD")
    if exif_ifd:
        for key in ("DateTimeOriginal", "CreateDate"):
            value = exif_ifd.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                parsed = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue

            offset_str = exif_ifd.get("OffsetTimeOriginal")
            tz_info = None
            if isinstance(offset_str, str) and offset_str:
                try:
                    tz_info = datetime.strptime(offset_str, "%z").tzinfo
                except ValueError:
                    tz_info = None

            if tz_info is None:
                tz_info = gettz() or datetime.now().astimezone().tzinfo or timezone.utc

            parsed = parsed.replace(tzinfo=tz_info)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return None


def _extract_content_id_from_exiftool(meta: Dict[str, Any]) -> Optional[str]:
    """Extract the Apple ``ContentIdentifier`` used for Live Photo pairing."""

    apple_group = _extract_group(meta, "Apple")
    if apple_group:
        content_id = apple_group.get("ContentIdentifier")
        if isinstance(content_id, str) and content_id:
            return content_id
    return None


def read_image_meta_with_exiftool(
    path: Path, metadata: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Read metadata for ``path`` using a pre-fetched ExifTool payload."""

    info = _empty_image_info()
    exif_payload: Optional[Any] = None

    if isinstance(metadata, dict):
        file_group = _extract_group(metadata, "File")
        if file_group:
            # Prefer geometry reported by ExifTool because it is already
            # available from the batch query and saves us from opening the file
            # with Pillow on every scan.
            width = file_group.get("ImageWidth")
            height = file_group.get("ImageHeight")
            mime = file_group.get("MIMEType")

            if isinstance(width, (int, float, str)):
                try:
                    info["w"] = int(float(width))
                except (TypeError, ValueError):
                    info["w"] = None
            if isinstance(height, (int, float, str)):
                try:
                    info["h"] = int(float(height))
                except (TypeError, ValueError):
                    info["h"] = None
            if isinstance(mime, str):
                info["mime"] = mime or None

        gps_payload = _extract_gps_from_exiftool(metadata)
        if gps_payload is not None:
            info["gps"] = gps_payload

        dt_value = _extract_datetime_from_exiftool(metadata)
        if dt_value:
            info["dt"] = dt_value

        content_id = _extract_content_id_from_exiftool(metadata)
        if content_id:
            info["content_id"] = content_id

    geometry_missing = info["w"] is None or info["h"] is None
    need_dt_fallback = info["dt"] is None

    if (geometry_missing or need_dt_fallback) and Image is not None and UnidentifiedImageError is not None:
        print(f"Opening image for metadata: {path}")
        try:
            with Image.open(path) as img:
                if geometry_missing:
                    # Only fall back to Pillow when ExifTool could not supply
                    # the dimensions, keeping the happy-path fast.
                    info["w"] = img.width
                    info["h"] = img.height
                    if info["mime"] is None:
                        info["mime"] = Image.MIME.get(img.format, None)
                if need_dt_fallback:
                    exif_payload = img.getexif() if hasattr(img, "getexif") else None
        except UnidentifiedImageError as exc:
            raise ExternalToolError(f"Unable to read image metadata for {path}") from exc
        except OSError as exc:
            raise ExternalToolError(f"OS error while reading {path}: {exc}") from exc

    if info["dt"] is None and exif_payload:
        fallback_dt = exif_payload.get(36867) or exif_payload.get(306)
        if isinstance(fallback_dt, str):
            info["dt"] = _normalise_exif_datetime(fallback_dt, exif_payload)

    if info["gps"]:
        gps = info["gps"]
        print(
            "Extracted GPS coordinates via ExifTool: "
            f"lat={gps['lat']:.6f}, lon={gps['lon']:.6f}"
        )
    else:
        print("No GPS coordinates found in metadata")

    return info


def read_image_meta(path: Path) -> Dict[str, Any]:
    """Compatibility wrapper that fetches ExifTool data for a single image."""

    metadata_block: Optional[Dict[str, Any]] = None
    try:
        payload = get_metadata_batch([path])
        if payload:
            metadata_block = payload[0]
    except ExternalToolError as exc:
        print(f"Warning: Could not use ExifTool for {path}: {exc}")

    return read_image_meta_with_exiftool(path, metadata_block)


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

    # Live Photo companion videos often expose the content identifier either at
    # the container (format) level or within individual streams. We inspect both
    # so the pairing logic remains stable even if ffprobe changes where it emits
    # the tag.
    if isinstance(fmt, dict):
        top_level_tags = fmt.get("tags")
        if isinstance(top_level_tags, dict) and not info.get("content_id"):
            content_id = top_level_tags.get("com.apple.quicktime.content.identifier")
            if isinstance(content_id, str) and content_id:
                info["content_id"] = content_id

    streams = metadata.get("streams", []) if isinstance(metadata, dict) else []
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue

            tag_payload = stream.get("tags")
            tags = tag_payload if isinstance(tag_payload, dict) else {}
            if tags and not info.get("content_id"):
                content_id = tags.get("com.apple.quicktime.content.identifier")
                if isinstance(content_id, str) and content_id:
                    info["content_id"] = content_id

            codec_type = stream.get("codec_type")
            if codec_type == "video":
                codec = stream.get("codec_name")
                if isinstance(codec, str):
                    info["codec"] = codec

                width = stream.get("width")
                height = stream.get("height")
                if isinstance(width, int) and isinstance(height, int):
                    info["w"] = width
                    info["h"] = height

                if tags:
                    still_time = tags.get("com.apple.quicktime.still-image-time")
                    if isinstance(still_time, str):
                        try:
                            info["still_image_time"] = float(still_time)
                        except ValueError:
                            info["still_image_time"] = None
            elif codec_type == "audio":
                codec = stream.get("codec_name")
                if isinstance(codec, str) and not info.get("codec"):
                    info["codec"] = codec
    return info


__all__ = ["read_image_meta", "read_image_meta_with_exiftool", "read_video_meta"]
