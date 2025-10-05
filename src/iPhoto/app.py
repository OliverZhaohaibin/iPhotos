"""High-level application facade."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List

from .cache.index_store import IndexStore
from .cache.lock import FileLock
from .config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE, WORK_DIR_NAME
from .core.pairing import pair_live
from .models.album import Album
from .models.types import LiveGroup
from .errors import ManifestInvalidError
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

    album = Album.open(root)
    include = album.manifest.get("filters", {}).get("include", DEFAULT_INCLUDE)
    exclude = album.manifest.get("filters", {}).get("exclude", DEFAULT_EXCLUDE)
    from .io.scanner import scan_album

    rows = list(scan_album(root, include, exclude))
    IndexStore(root).write_rows(rows)
    _ensure_links(root, rows)
    return rows


def pair(root: Path) -> List[LiveGroup]:
    """Rebuild live photo pairings from the current index."""

    rows = list(IndexStore(root).read_all())
    groups, payload = _compute_links_payload(rows)
    _write_links(root, payload)
    return groups
