"""Metadata readers for still images and video clips."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional

from dateutil.parser import isoparse
from dateutil.tz import gettz

from ..errors import ExternalToolError
from ..utils.deps import load_pillow
from ..utils.exiftool import get_metadata_batch
from ..utils.ffmpeg import probe_media
from ..utils.logging import get_logger

_PILLOW = load_pillow()

if _PILLOW is not None:
    Image = _PILLOW.Image
    UnidentifiedImageError = _PILLOW.UnidentifiedImageError
else:  # pragma: no cover - exercised only when Pillow is missing
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = None  # type: ignore[assignment]

LOGGER = get_logger()


def _empty_media_info() -> Dict[str, Any]:
    """Return a metadata stub used whenever inspection fails."""

    return {
        "w": None,
        "h": None,
        "mime": None,
        "dt": None,
        "make": None,
        "model": None,
        "lens": None,
        "iso": None,
        "f_number": None,
        "exposure_time": None,
        "exposure_compensation": None,
        "focal_length": None,
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


def _coerce_fractional(value: Any) -> Optional[float]:
    """Return ``value`` as ``float`` while accepting rational strings."""

    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        candidate = candidate.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
        matches = list(re.finditer(r"-?\d+(?:/\d+|\.\d+)?", candidate))
        if not matches:
            return None
        total = 0.0
        parsed_any = False
        for match in matches:
            token = match.group(0)
            try:
                if "/" in token:
                    total += float(Fraction(token))
                else:
                    total += float(token)
                parsed_any = True
            except (ValueError, ZeroDivisionError):
                continue
        return total if parsed_any else None
    return None


def _pick_string(*candidates: Any) -> Optional[str]:
    """Return the first non-empty string from ``candidates``."""

    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip()
            if normalized:
                return normalized
    return None


def _extract_group(metadata: Dict[str, Any], group_name: str) -> Optional[Dict[str, Any]]:
    """Return an ExifTool group mapping from either nested or flattened layouts."""

    # ``exiftool`` can emit nested dictionaries (when ``-g1`` is supplied) or a
    # flat mapping of ``Group:Tag`` keys depending on the version and flags in
    # use.  This helper normalises both forms so downstream code can treat the
    # result as a simple dictionary of tag names.
    group = metadata.get(group_name)
    if isinstance(group, dict):
        return group

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

    quicktime = _extract_group(meta, "QuickTime")
    if quicktime:
        lat = _coerce_decimal(quicktime.get("GPSLatitude"))
        lon = _coerce_decimal(quicktime.get("GPSLongitude"))
        if lat is not None and lon is not None:
            return {"lat": lat, "lon": lon}

        iso6709 = quicktime.get("LocationISO6709")
        if isinstance(iso6709, str):
            # Some devices only publish a single ISO 6709 string (for example
            # ``+51.5080-0.1400/``).  The regex extracts the latitude and
            # longitude so they can be converted to floating point numbers.
            match = re.match(r"([+-]\d+\.\d+)([+-]\d+\.\d+)", iso6709)
            if match:
                try:
                    lat, lon = (float(component) for component in match.groups())
                    return {"lat": lat, "lon": lon}
                except (TypeError, ValueError):
                    LOGGER.debug("Failed to parse QuickTime ISO6709 string: %s", iso6709)

    iso6709_fallback = meta.get("com.apple.quicktime.location.ISO6709")
    if isinstance(iso6709_fallback, str):
        match = re.match(r"([+-]\d+\.\d+)([+-]\d+\.\d+)", iso6709_fallback)
        if match:
            try:
                lat, lon = (float(component) for component in match.groups())
                return {"lat": lat, "lon": lon}
            except (TypeError, ValueError):
                LOGGER.debug("Failed to parse QuickTime ISO6709 fallback: %s", iso6709_fallback)

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

    quicktime = _extract_group(meta, "QuickTime")
    if quicktime:
        for key in ("CreateDate", "ModifyDate"):
            value = quicktime.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                parts = value.split(" ")
                if len(parts) > 1 and ":" in parts[0]:
                    parts[0] = parts[0].replace(":", "-")
                    value = " ".join(parts)
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

    quicktime = _extract_group(meta, "QuickTime")
    if quicktime:
        content_id = quicktime.get("ContentIdentifier")
        if isinstance(content_id, str) and content_id:
            return content_id

    return None


def read_image_meta_with_exiftool(
    path: Path, metadata: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Read metadata for ``path`` using a pre-fetched ExifTool payload."""

    info = _empty_media_info()
    exif_payload: Optional[Any] = None

    if isinstance(metadata, dict):
        exif_group = _extract_group(metadata, "EXIF") or {}
        ifd0_group = _extract_group(metadata, "IFD0") or {}
        exif_ifd_group = _extract_group(metadata, "ExifIFD") or {}
        maker_notes_group = _extract_group(metadata, "MakerNotes") or {}
        composite_group = _extract_group(metadata, "Composite") or {}
        quicktime_group = _extract_group(metadata, "QuickTime") or {}
        xmp_group = _extract_group(metadata, "XMP") or {}

        file_group = _extract_group(metadata, "File")
        if file_group:
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

        # Camera make and model fields are spread across multiple ExifTool groups
        # depending on the originating device (EXIF/IFD0 for still images,
        # QuickTime for iOS videos, etc.). We therefore walk through each
        # candidate group in descending priority to capture a non-empty value.
        make_value = _pick_string(
            info.get("make"),
            ifd0_group.get("Make"),
            exif_group.get("Make"),
            maker_notes_group.get("Make"),
            quicktime_group.get("Make"),
            xmp_group.get("Make"),
        )
        if make_value is not None:
            info["make"] = make_value

        model_value = _pick_string(
            info.get("model"),
            ifd0_group.get("Model"),
            exif_group.get("Model"),
            maker_notes_group.get("Model"),
            composite_group.get("Model"),
            quicktime_group.get("Model"),
            xmp_group.get("Model"),
        )
        if model_value is not None:
            info["model"] = model_value

        lens_value = _pick_string(
            exif_ifd_group.get("LensModel"),
            exif_group.get("LensModel"),
            maker_notes_group.get("LensType"),
            maker_notes_group.get("LensModel"),
            composite_group.get("LensID"),
            composite_group.get("Lens"),
            composite_group.get("LensInfo"),
            xmp_group.get("Lens"),
            xmp_group.get("LensModel"),
        )
        if lens_value is not None:
            info["lens"] = lens_value

        iso_value = _coerce_decimal(exif_ifd_group.get("ISO"))
        if iso_value is None:
            iso_value = _coerce_decimal(exif_group.get("ISO"))
        if iso_value is None:
            iso_value = _coerce_decimal(quicktime_group.get("ISO"))
        if iso_value is not None:
            info["iso"] = int(round(iso_value))

        f_number_value = _coerce_fractional(exif_ifd_group.get("FNumber"))
        if f_number_value is None:
            f_number_value = _coerce_fractional(exif_group.get("FNumber"))
        if f_number_value is None:
            f_number_value = _coerce_fractional(composite_group.get("Aperture"))
        if f_number_value is not None:
            info["f_number"] = f_number_value

        exposure_time_value = _coerce_fractional(exif_ifd_group.get("ExposureTime"))
        if exposure_time_value is None:
            exposure_time_value = _coerce_fractional(composite_group.get("ShutterSpeed"))
        if exposure_time_value is None:
            exposure_time_value = _coerce_fractional(quicktime_group.get("ExposureTime"))
        if exposure_time_value is not None:
            info["exposure_time"] = exposure_time_value

        exposure_comp_value = _coerce_fractional(
            exif_ifd_group.get("ExposureCompensation")
        )
        if exposure_comp_value is None:
            exposure_comp_value = _coerce_fractional(
                exif_ifd_group.get("ExposureBiasValue")
            )
        if exposure_comp_value is None:
            exposure_comp_value = _coerce_fractional(
                composite_group.get("ExposureCompensation")
            )
        if exposure_comp_value is None:
            exposure_comp_value = _coerce_fractional(
                quicktime_group.get("ExposureCompensation")
            )
        if exposure_comp_value is not None:
            info["exposure_compensation"] = exposure_comp_value

        focal_length_value = _coerce_fractional(
            exif_ifd_group.get("FocalLength")
        )
        if focal_length_value is None:
            focal_length_value = _coerce_fractional(
                composite_group.get("FocalLength")
            )
        if focal_length_value is None:
            focal_length_value = _coerce_fractional(
                quicktime_group.get("FocalLength")
            )
        if focal_length_value is not None:
            info["focal_length"] = focal_length_value

    geometry_missing = info["w"] is None or info["h"] is None
    need_dt_fallback = info["dt"] is None

    if (geometry_missing or need_dt_fallback) and Image is not None and UnidentifiedImageError is not None:
        LOGGER.debug("Opening %s with Pillow to backfill metadata", path)
        try:
            with Image.open(path) as img:
                if geometry_missing:
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

    return info


def read_image_meta(path: Path) -> Dict[str, Any]:
    """Compatibility wrapper that fetches ExifTool data for a single image."""

    metadata_block: Optional[Dict[str, Any]] = None
    try:
        payload = get_metadata_batch([path])
        if payload:
            metadata_block = payload[0]
    except ExternalToolError as exc:
        LOGGER.warning("Could not use ExifTool for %s: %s", path, exc)

    return read_image_meta_with_exiftool(path, metadata_block)


def read_video_meta(path: Path, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return metadata for a video file, enriching it with ExifTool payloads."""

    info = _empty_media_info()
    info["mime"] = "video/quicktime" if path.suffix.lower() in {".mov", ".qt"} else "video/mp4"
    info["dur"] = None
    info["codec"] = None
    info["still_image_time"] = None

    if isinstance(metadata, dict):
        gps_payload = _extract_gps_from_exiftool(metadata)
        if gps_payload is not None:
            info["gps"] = gps_payload

        dt_value = _extract_datetime_from_exiftool(metadata)
        if dt_value:
            info["dt"] = dt_value

        content_id = _extract_content_id_from_exiftool(metadata)
        if content_id:
            info["content_id"] = content_id

    try:
        ffprobe_meta = probe_media(path)
    except ExternalToolError:
        return info

    fmt = ffprobe_meta.get("format", {}) if isinstance(ffprobe_meta, dict) else {}
    duration = fmt.get("duration") if isinstance(fmt, dict) else None
    if isinstance(duration, str):
        try:
            info["dur"] = float(duration)
        except ValueError:
            info["dur"] = None

    if isinstance(fmt, dict):
        top_level_tags = fmt.get("tags")
        if isinstance(top_level_tags, dict) and not info.get("content_id"):
            content_id = top_level_tags.get("com.apple.quicktime.content.identifier")
            if isinstance(content_id, str) and content_id:
                info["content_id"] = content_id

    streams = ffprobe_meta.get("streams", []) if isinstance(ffprobe_meta, dict) else []
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue

            tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
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
