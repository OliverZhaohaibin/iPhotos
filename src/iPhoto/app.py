"""High-level application facade."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List

from .cache.index_store import IndexStore
from .cache.lock import FileLock
from .config import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    WORK_DIR_NAME,
    RECENTLY_DELETED_DIR_NAME,
)
from .core.pairing import pair_live
from .models.album import Album
from .models.types import LiveGroup
from .errors import IndexCorruptedError, ManifestInvalidError
from .utils.jsonio import read_json, write_json
from .utils.logging import get_logger

LOGGER = get_logger()


def open_album(root: Path) -> Album:
    """Open an album directory, scanning and pairing as required."""

    album = Album.open(root)
    store = IndexStore(root)
    rows = list(store.read_all())
    if not rows:
        include = album.manifest.get("filters", {}).get("include", DEFAULT_INCLUDE)
        exclude = album.manifest.get("filters", {}).get("exclude", DEFAULT_EXCLUDE)
        from .io.scanner import scan_album

        rows = list(scan_album(root, include, exclude))
        store.write_rows(rows)
    _ensure_links(root, rows)
    return album


def _ensure_links(root: Path, rows: List[dict]) -> None:
    work_dir = root / WORK_DIR_NAME
    links_path = work_dir / "links.json"
    _, payload = _compute_links_payload(rows)
    if links_path.exists():
        try:
            existing: Dict[str, object] = read_json(links_path)
        except ManifestInvalidError:
            existing = {}
        if existing == payload:
            return
    LOGGER.info("Updating links.json for %s", root)
    _write_links(root, payload)


def _compute_links_payload(rows: List[dict]) -> tuple[List[LiveGroup], Dict[str, object]]:
    groups = pair_live(rows)
    payload: Dict[str, object] = {
        "schema": "iPhoto/links@1",
        "live_groups": [asdict(group) for group in groups],
        "clips": [],
    }
    return groups, payload


def _write_links(root: Path, payload: Dict[str, object]) -> None:
    work_dir = root / WORK_DIR_NAME
    with FileLock(root, "links"):
        write_json(work_dir / "links.json", payload, backup_dir=work_dir / "manifest.bak")


def rescan(root: Path) -> List[dict]:
    """Rescan the album and return the fresh index rows."""

    store = IndexStore(root)

    # ``original_rel_path`` is only populated for assets in the shared trash
    # album.  Rescanning that directory must therefore preserve the existing
    # mapping so the restore feature still knows where each item originated.
    is_recently_deleted = root.name == RECENTLY_DELETED_DIR_NAME
    preserved_restore_paths: Dict[str, str] = {}
    if is_recently_deleted:
        try:
            for row in store.read_all():
                rel_value = row.get("rel")
                original_rel = row.get("original_rel_path")
                if not isinstance(rel_value, str) or not isinstance(original_rel, str):
                    continue
                rel_key = Path(rel_value).as_posix()
                preserved_restore_paths[rel_key] = original_rel
        except IndexCorruptedError:
            # A corrupted index means we cannot recover historical restore
            # targets.  Emit a warning and continue with a clean rescan so new
            # trash entries still receive restore metadata.
            LOGGER.warning("Unable to read previous trash index for %s", root)

    album = Album.open(root)
    include = album.manifest.get("filters", {}).get("include", DEFAULT_INCLUDE)
    exclude = album.manifest.get("filters", {}).get("exclude", DEFAULT_EXCLUDE)
    from .io.scanner import scan_album

    rows = list(scan_album(root, include, exclude))
    if is_recently_deleted and preserved_restore_paths:
        for new_row in rows:
            rel_value = new_row.get("rel")
            if not isinstance(rel_value, str):
                continue
            rel_key = Path(rel_value).as_posix()
            preserved = preserved_restore_paths.get(rel_key)
            if preserved and not new_row.get("original_rel_path"):
                new_row["original_rel_path"] = preserved

    store.write_rows(rows)
    _ensure_links(root, rows)
    return rows


def pair(root: Path) -> List[LiveGroup]:
    """Rebuild live photo pairings from the current index."""

    rows = list(IndexStore(root).read_all())
    groups, payload = _compute_links_payload(rows)
    _write_links(root, payload)
    return groups
