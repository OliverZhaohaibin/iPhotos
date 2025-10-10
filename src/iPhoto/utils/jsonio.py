"""Helpers for JSON input/output with atomic writes and backups."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ManifestInvalidError


def read_json(path: Path) -> dict[str, Any]:
    """Read JSON from *path* and return a dictionary."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ManifestInvalidError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestInvalidError(f"Invalid JSON data in {path}") from exc


def atomic_write_text(path: Path, data: str) -> None:
    """Atomically write *data* into *path*."""

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    # ``Path.replace`` can intermittently fail on Windows when another process briefly
    # touches either the destination or the temporary file (for instance antivirus or
    # indexing services).  Retrying with a short back-off and removing any stale
    # destination file makes the operation resilient while still providing an atomic
    # swap when the replacement finally succeeds.
    last_exc: PermissionError | None = None
    for attempt in range(5):
        try:
            tmp_path.replace(path)
            break
        except PermissionError as exc:
            last_exc = exc
            if path.exists():
                try:
                    path.unlink()
                except PermissionError:
                    # If the destination is locked keep retrying – a later attempt
                    # may succeed once the lock is released.
                    pass
            if attempt == 4:
                tmp_path.unlink(missing_ok=True)
                raise exc
            time.sleep(0.05 * (attempt + 1))
    else:  # pragma: no cover - defensive guard; loop always breaks or raises
        if last_exc is not None:
            raise last_exc


def _write_backup(path: Path, backup_dir: Path) -> None:
    if not path.exists():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{timestamp}{path.suffix}"
    backup_path.write_bytes(path.read_bytes())


def write_json(path: Path, data: dict[str, Any], *, backup_dir: Path | None = None) -> None:
    """Write *data* into *path* atomically with optional backups."""

    if backup_dir is not None:
        _write_backup(path, backup_dir)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_text(path, payload)
