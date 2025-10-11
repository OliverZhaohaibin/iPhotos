"""Directory scanner producing index rows."""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from ..config import WORK_DIR_NAME
from ..errors import ExternalToolError
from ..utils.exiftool import get_metadata_batch
from ..utils.hashutils import file_xxh3
from ..utils.pathutils import ensure_work_dir, is_excluded, should_include
from .metadata import read_image_meta, read_image_meta_with_exiftool, read_video_meta

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
        # Surface the problem but continue scanning so at least a minimal
        # index can be produced for the affected files.
        print(f"Failed to get metadata for images: {exc}")
        image_metadata_payloads = []

    metadata_lookup: Dict[Path, Dict[str, Any]] = {}
    for payload in image_metadata_payloads:
        source = payload.get("SourceFile")
        if isinstance(source, str):
            metadata_lookup[Path(source).resolve()] = payload

    for image_path in image_paths:
        metadata = metadata_lookup.get(image_path.resolve())
        yield _build_row(root, image_path, metadata)

    for video_path in video_paths:
        yield _build_row(root, video_path)


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


def _build_row(
    root: Path,
    file_path: Path,
    metadata_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return an index row for ``file_path``.

    Parameters
    ----------
    root:
        Root directory of the album being scanned. Used to compute the
        relative path stored in the index entry.
    file_path:
        Absolute path to the asset that should be indexed.
    metadata_override:
        Optional ExifTool payload that has already been resolved for
        ``file_path``. When provided (e.g. during batch scans) we skip the
        additional lookup to avoid spawning a separate ExifTool process.
    """

    stat = file_path.stat()
    base_row = _build_base_row(root, file_path, stat)

    suffix = file_path.suffix.lower()
    metadata: Dict[str, Any] = {}

    if suffix in _IMAGE_EXTENSIONS:
        payload = metadata_override
        if payload is None:
            try:
                payloads = get_metadata_batch([file_path])
                payload = payloads[0] if payloads else None
            except ExternalToolError as exc:
                print(f"Failed to fetch ExifTool metadata for {file_path}: {exc}")
                payload = None
        metadata = read_image_meta_with_exiftool(file_path, payload)
    elif suffix in _VIDEO_EXTENSIONS:
        metadata = read_video_meta(file_path)
    else:
        # For unknown formats fall back to the simple Pillow-based reader so
        # the caller still receives geometry and timestamp information.
        metadata = read_image_meta(file_path)

    for key, value in metadata.items():
        if value is None and key in base_row:
            continue
        base_row[key] = value

    return base_row
