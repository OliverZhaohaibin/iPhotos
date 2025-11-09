"""High-level application facade."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

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


def _normalise_rel_key(rel_value: object) -> Optional[str]:
    """Return a POSIX-formatted representation of *rel_value* when possible.

    The index uses the relative path under the album root as its stable
    identifier.  Callers occasionally pass :class:`pathlib.Path` instances while
    other code paths hand over plain strings.  Normalising via
    :meth:`Path.as_posix` collapses both representations into a single canonical
    form so lookups remain stable regardless of the originating caller or the
    underlying operating system.
    """

    if isinstance(rel_value, str) and rel_value:
        return Path(rel_value).as_posix()
    if isinstance(rel_value, Path):
        return rel_value.as_posix()
    if rel_value:
        return Path(str(rel_value)).as_posix()
    return None


def _update_index_snapshot(root: Path, materialised_rows: List[dict]) -> None:
    """Apply *materialised_rows* to ``index.jsonl`` using incremental updates.

    Instead of rewriting the entire index after every scan, the helper removes
    entries that are no longer present and upserts any rows that appeared or
    changed.  The incremental approach keeps writes small, which in turn reduces
    churn on networked or slow storage while still guaranteeing atomicity
    through the :class:`~iPhoto.cache.index_store.IndexStore` primitives.
    """

    store = IndexStore(root)

    existing_rows: Dict[str, dict] = {}
    corrupted_during_read = False
    try:
        for cached_row in store.read_all():
            rel_key = _normalise_rel_key(cached_row.get("rel"))
            if rel_key is None:
                continue
            existing_rows[rel_key] = cached_row
    except IndexCorruptedError:
        existing_rows = {}
        corrupted_during_read = True

    fresh_rows: Dict[str, dict] = {}
    for row in materialised_rows:
        rel_key = _normalise_rel_key(row.get("rel"))
        if rel_key is None:
            continue
        fresh_rows[rel_key] = row

    materialised_snapshot = list(fresh_rows.values())

    if corrupted_during_read:
        store.write_rows(materialised_snapshot)
        return

    if not fresh_rows and not existing_rows:
        return

    stale_rels = set(existing_rows.keys()) - set(fresh_rows.keys())
    if stale_rels:
        try:
            store.remove_rows(stale_rels)
        except IndexCorruptedError:
            store.write_rows(materialised_snapshot)
            return

    updated_payload: List[dict] = []
    for rel_key, row in fresh_rows.items():
        cached = existing_rows.get(rel_key)
        if cached is None or cached != row:
            updated_payload.append(row)

    if updated_payload:
        try:
            store.append_rows(updated_payload)
        except IndexCorruptedError:
            store.write_rows(materialised_snapshot)


def rescan(root: Path) -> List[dict]:
    """Rescan the album and return the fresh index rows."""

    store = IndexStore(root)

    # ``original_rel_path`` is only populated for assets in the shared trash
    # album.  Rescanning that directory must therefore preserve the existing
    # mapping so the restore feature still knows where each item originated.
    is_recently_deleted = root.name == RECENTLY_DELETED_DIR_NAME
    preserved_fields = (
        "original_rel_path",
        "original_album_id",
        "original_album_subpath",
    )
    preserved_restore_rows: Dict[str, dict] = {}
    if is_recently_deleted:
        try:
            for row in store.read_all():
                rel_value = row.get("rel")
                if not isinstance(rel_value, str):
                    continue
                if not any(field in row for field in preserved_fields):
                    continue
                rel_key = Path(rel_value).as_posix()
                preserved_restore_rows[rel_key] = row
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
    if is_recently_deleted and preserved_restore_rows:
        for new_row in rows:
            rel_value = new_row.get("rel")
            if not isinstance(rel_value, str):
                continue
            rel_key = Path(rel_value).as_posix()
            cached = preserved_restore_rows.get(rel_key)
            if not cached:
                continue
            for field in preserved_fields:
                if field in cached and field not in new_row:
                    new_row[field] = cached[field]

    _update_index_snapshot(root, rows)
    _ensure_links(root, rows)
    return rows


def pair(root: Path) -> List[LiveGroup]:
    """Rebuild live photo pairings from the current index."""

    rows = list(IndexStore(root).read_all())
    groups, payload = _compute_links_payload(rows)
    _write_links(root, payload)
    return groups
