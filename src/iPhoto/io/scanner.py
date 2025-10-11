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
from .metadata import read_image_meta_with_exiftool, read_video_meta

_IMAGE_EXTENSIONS = {".heic", ".jpg", ".jpeg", ".png"}
_VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".qt"}


def gather_media_paths(
    root: Path, include_globs: Iterable[str], exclude_globs: Iterable[str]
) -> Tuple[List[Path], List[Path]]:
    """Collect candidate media files before metadata processing.

    Separating discovery from processing enables the caller (typically the
    UI worker) to know how many files need work, which in turn allows an
    accurate progress bar.  The function returns two lists so images and
    videos can be handled using their respective metadata pipelines.
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
    """Yield populated index rows for the provided media paths.

    Images are processed in a batch so we can reuse a single ExifTool
    invocation.  This is significantly faster than launching a new process
    per image and also guarantees consistent locale handling because the
    metadata payloads share a single execution context.
    """

    try:
        image_metadata_payloads = get_metadata_batch(image_paths)
    except ExternalToolError as exc:
        # Expose the failure so operators understand why GPS/location data may
        # be missing, but continue producing rows so the scan still completes.
        print(f"Failed to get metadata for images: {exc}")
        image_metadata_payloads = []

    metadata_lookup: Dict[Path, Dict[str, Any]] = {}
    for idx, payload in enumerate(image_metadata_payloads):
        if not isinstance(payload, dict):
            continue

        if idx < len(image_paths):
            metadata_lookup[image_paths[idx]] = payload

        source = payload.get("SourceFile")
        if isinstance(source, str):
            # Resolving the path reported by ExifTool protects us against
            # casing differences and symlink resolution quirks across
            # platforms.  Registering both keys keeps lookups stable.
            metadata_lookup[Path(source).resolve()] = payload

    for image_path in image_paths:
        metadata = metadata_lookup.get(image_path)
        if metadata is None:
            metadata = metadata_lookup.get(image_path.resolve())
        yield _build_row(root, image_path, metadata)

    for video_path in video_paths:
        yield _build_row(root, video_path)


def scan_album(
    root: Path,
    include_globs: Iterable[str],
    exclude_globs: Iterable[str],
) -> Iterator[Dict[str, Any]]:
    """Yield index rows for all matching assets in *root*.

    The default CLI entry points rely on this helper.  It remains as a thin
    wrapper so existing call sites keep working, while new code (the GUI
    scanner worker) can directly call :func:`gather_media_paths` and
    :func:`process_media_paths` for more granular progress control.
    """

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
    """Return an index row for ``file_path``.

    Parameters
    ----------
    root:
        Album directory currently being scanned.  Needed to compute
        ``rel`` (the path stored in the index file).
    file_path:
        Fully-qualified path to the media item that should be indexed.
    metadata_override:
        Optional ExifTool payload that has already been fetched for this
        file.  When ``None`` the helper falls back to running a dedicated
        batch so callers outside the batch pipeline still succeed.
    """

    stat = file_path.stat()
    base_row = _build_base_row(root, file_path, stat)

    suffix = file_path.suffix.lower()
    metadata: Dict[str, Any]

    if suffix in _IMAGE_EXTENSIONS:
        metadata = read_image_meta_with_exiftool(file_path, metadata_override)
    elif suffix in _VIDEO_EXTENSIONS:
        metadata = read_video_meta(file_path)
    else:
        # Unsupported file types still contribute their basic filesystem
        # information.  We intentionally avoid raising so scans remain robust
        # even when albums contain stray helper documents.
        metadata = {}

    for key, value in metadata.items():
        if value is None and key in base_row:
            continue
        base_row[key] = value

    return base_row
