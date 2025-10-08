"""Media type classification helpers shared by UI models."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Tuple

IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".heifs",
    ".heicf",
})

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mov",
    ".mp4",
    ".m4v",
    ".qt",
    ".avi",
    ".wmv",
    ".mkv",
})


def _normalise_mime(value: object) -> str:
    """Return a lower-case MIME type string or an empty string."""

    if isinstance(value, str):
        return value.strip().lower()
    return ""


def _suffix_from_row(row: Mapping[str, object]) -> str:
    """Extract a normalised file suffix from *row* if available."""

    rel = row.get("rel")
    if isinstance(rel, Path):
        return rel.suffix.lower()
    if isinstance(rel, str):
        return Path(rel).suffix.lower()
    return ""


def classify_media(row: Mapping[str, object]) -> Tuple[bool, bool]:
    """Return booleans indicating whether *row* describes an image or video.

    The function inspects MIME types, legacy ``type`` fields, and file
    extensions in order of preference. Additional video formats beyond the
    default MP4/MOV set are supported to handle albums with mixed footage.
    """

    mime = _normalise_mime(row.get("mime"))
    if mime.startswith("image/"):
        return True, False
    if mime.startswith("video/"):
        return False, True

    legacy_kind = row.get("type")
    if isinstance(legacy_kind, str):
        kind = legacy_kind.strip().lower()
        if kind == "image":
            return True, False
        if kind == "video":
            return False, True

    suffix = _suffix_from_row(row)
    if suffix in IMAGE_EXTENSIONS:
        return True, False
    if suffix in VIDEO_EXTENSIONS:
        return False, True
    return False, False


__all__ = ["classify_media", "IMAGE_EXTENSIONS", "VIDEO_EXTENSIONS"]
