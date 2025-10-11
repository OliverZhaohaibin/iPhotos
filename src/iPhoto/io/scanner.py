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
from ..utils.logging import get_logger
from ..utils.pathutils import ensure_work_dir, is_excluded, should_include
from .metadata import read_image_meta_with_exiftool, read_video_meta

_IMAGE_EXTENSIONS = {".heic", ".jpg", ".jpeg", ".png"}
_VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".qt"}

LOGGER = get_logger()


def gather_media_paths(
    root: Path, include_globs: Iterable[str], exclude_globs: Iterable[str]
) -> Tuple[List[Path], List[Path]]:
    """Collect media files that should be indexed.

    Separating discovery from processing allows callers to present accurate
    progress indicators, because the total work is known before any metadata
    extraction begins.
    """

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


def process_media_paths(
    root: Path, image_paths: List[Path], video_paths: List[Path]
) -> Iterator[Dict[str, Any]]:
    """Yield populated index rows for the provided media paths."""

    all_paths = image_paths + video_paths
    try:
        metadata_payloads = get_metadata_batch(all_paths)
    except ExternalToolError as exc:
        LOGGER.warning("Batch ExifTool query failed for %s files: %s", len(all_paths), exc)
        metadata_payloads = []

    metadata_lookup: Dict[Path, Dict[str, Any]] = {}
    for payload in metadata_payloads:
        if not isinstance(payload, dict):
            continue

        source = payload.get("SourceFile")
        if isinstance(source, str):
            source_path = Path(source)
            # Register both the raw path reported by ExifTool and the resolved
            # absolute path so lookups succeed regardless of how the caller
            # constructed the candidate list.
            metadata_lookup[source_path] = payload
            metadata_lookup[source_path.resolve()] = payload

    for path in all_paths:
        resolved = path.resolve()
        metadata = metadata_lookup.get(resolved)
        if metadata is None:
            metadata = metadata_lookup.get(path)
        yield _build_row(root, path, metadata)


def scan_album(
    root: Path,
    include_globs: Iterable[str],
    exclude_globs: Iterable[str],
) -> Iterator[Dict[str, Any]]:
    """Yield index rows for all matching assets in *root*."""

    ensure_work_dir(root, WORK_DIR_NAME)
    image_paths, video_paths = gather_media_paths(root, include_globs, exclude_globs)
    yield from process_media_paths(root, image_paths, video_paths)


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
    """Return an index row for ``file_path``."""

    stat = file_path.stat()
    base_row = _build_base_row(root, file_path, stat)

    suffix = file_path.suffix.lower()
    metadata: Dict[str, Any]

    if suffix in _IMAGE_EXTENSIONS:
        metadata = read_image_meta_with_exiftool(file_path, metadata_override)
    elif suffix in _VIDEO_EXTENSIONS:
        metadata = read_video_meta(file_path, metadata_override)
    else:
        metadata = {}

    for key, value in metadata.items():
        if value is None and key in base_row:
            continue
        base_row[key] = value

    return base_row
