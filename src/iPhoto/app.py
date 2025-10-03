"""High-level application facade."""

from __future__ import annotations

from pathlib import Path
from typing import List

from .cache.index_store import IndexStore
from .cache.lock import FileLock
from .config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE, WORK_DIR_NAME
from .core.pairing import pair_live
from .models.album import Album
from .models.types import LiveGroup
from .utils.jsonio import write_json
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
    if links_path.exists():
        return
    LOGGER.info("Generating links.json for %s", root)
    groups = pair_live(rows)
    payload = {
        "schema": "iPhoto/links@1",
        "live_groups": [group.__dict__ for group in groups],
        "clips": [],
    }
    with FileLock(root, "links"):
        write_json(links_path, payload, backup_dir=work_dir / "manifest.bak")


def rescan(root: Path) -> List[dict]:
    """Rescan the album and return the fresh index rows."""

    album = Album.open(root)
    include = album.manifest.get("filters", {}).get("include", DEFAULT_INCLUDE)
    exclude = album.manifest.get("filters", {}).get("exclude", DEFAULT_EXCLUDE)
    from .io.scanner import scan_album

    rows = list(scan_album(root, include, exclude))
    IndexStore(root).write_rows(rows)
    return rows


def pair(root: Path) -> List[LiveGroup]:
    """Rebuild live photo pairings from the current index."""

    rows = list(IndexStore(root).read_all())
    groups = pair_live(rows)
    work_dir = root / WORK_DIR_NAME
    payload = {
        "schema": "iPhoto/links@1",
        "live_groups": [group.__dict__ for group in groups],
        "clips": [],
    }
    with FileLock(root, "links"):
        write_json(work_dir / "links.json", payload, backup_dir=work_dir / "manifest.bak")
    return groups
