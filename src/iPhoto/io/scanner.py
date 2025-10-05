"""Directory scanner producing index rows."""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

from ..config import WORK_DIR_NAME
from ..utils.hashutils import file_xxh3
from ..utils.pathutils import ensure_work_dir, is_excluded, should_include
from .metadata import read_image_meta, read_video_meta


def _gather_file_paths(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def scan_album(
    root: Path,
    include_globs: Iterable[str],
    exclude_globs: Iterable[str],
) -> Iterator[Dict[str, Any]]:
    """Yield index rows for all matching assets in *root*."""

    ensure_work_dir(root, WORK_DIR_NAME)
    for file_path in _gather_file_paths(root):
        if WORK_DIR_NAME in file_path.parts:
            continue
        if is_excluded(file_path, exclude_globs, root=root):
            continue
        if not should_include(file_path, include_globs, exclude_globs, root=root):
            continue
        yield _build_row(root, file_path)


def _build_row(root: Path, file_path: Path) -> Dict[str, Any]:
    rel = file_path.relative_to(root).as_posix()
    stat = file_path.stat()
    base_row: Dict[str, Any] = {
        "rel": rel,
        "bytes": stat.st_size,
        "dt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "id": f"as_{file_xxh3(file_path)}",
        "mime": mimetypes.guess_type(file_path.name)[0],
    }
    lower = file_path.suffix.lower()
    metadata: Dict[str, Any] = {}
    if lower in {".heic", ".jpg", ".jpeg", ".png"}:
        metadata = read_image_meta(file_path)
    elif lower in {".mov", ".mp4", ".m4v", ".qt"}:
        metadata = read_video_meta(file_path)
    for key, value in metadata.items():
        if value is None and key in base_row:
            continue
        base_row[key] = value
    return base_row
