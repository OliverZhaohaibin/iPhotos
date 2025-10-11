"""Directory scanner producing index rows."""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Tuple

from ..config import WORK_DIR_NAME
from ..errors import ExternalToolError
from ..utils.exiftool import get_metadata_batch
from ..utils.hashutils import file_xxh3
from ..utils.pathutils import ensure_work_dir, is_excluded, should_include
from .metadata import read_image_meta_with_exiftool, read_video_meta

_IMAGE_EXTENSIONS = {".heic", ".jpg", ".jpeg", ".png"}
_VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".qt"}


def _gather_media_paths(
    root: Path, include_globs: Iterable[str], exclude_globs: Iterable[str]
) -> Tuple[List[Path], List[Path]]:
    """Return image and video paths that satisfy the inclusion rules."""

    image_paths: List[Path] = []
    video_paths: List[Path] = []

    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        if WORK_DIR_NAME in candidate.parts:
            continue
        if is_excluded(candidate, exclude_globs, root=root):
            continue
        if not should_include(candidate, include_globs, exclude_globs, root=root):
            continue

        suffix = candidate.suffix.lower()
        if suffix in _IMAGE_EXTENSIONS:
            image_paths.append(candidate)
        elif suffix in _VIDEO_EXTENSIONS:
            video_paths.append(candidate)

    return image_paths, video_paths


def scan_album(
    root: Path,
    include_globs: Iterable[str],
    exclude_globs: Iterable[str],
) -> Iterator[Dict[str, Any]]:
    """Yield index rows for all matching assets in *root*."""

    ensure_work_dir(root, WORK_DIR_NAME)
    image_paths, video_paths = _gather_media_paths(root, include_globs, exclude_globs)

    try:
        image_metadata_payloads = get_metadata_batch(image_paths)
    except ExternalToolError as exc:
        print(f"Failed to get metadata for images: {exc}")
        image_metadata_payloads = []

    metadata_lookup = {}
    for payload in image_metadata_payloads:
        source = payload.get("SourceFile")
        if isinstance(source, str):
            metadata_lookup[Path(source).resolve()] = payload

    for image_path in image_paths:
        stat = image_path.stat()
        base_row = _build_base_row(root, image_path, stat)
        metadata = metadata_lookup.get(image_path.resolve())
        enriched = read_image_meta_with_exiftool(image_path, metadata)
        for key, value in enriched.items():
            if value is None and key in base_row:
                continue
            base_row[key] = value
        yield base_row

    for video_path in video_paths:
        stat = video_path.stat()
        base_row = _build_base_row(root, video_path, stat)
        metadata = read_video_meta(video_path)
        for key, value in metadata.items():
            if value is None and key in base_row:
                continue
            base_row[key] = value
        yield base_row


def _build_base_row(root: Path, file_path: Path, stat: Any) -> Dict[str, Any]:
    """Create the common metadata fields shared by images and videos."""

    rel = file_path.relative_to(root).as_posix()
    return {
        "rel": rel,
        "bytes": stat.st_size,
        "dt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "id": f"as_{file_xxh3(file_path)}",
        "mime": mimetypes.guess_type(file_path.name)[0],
    }
