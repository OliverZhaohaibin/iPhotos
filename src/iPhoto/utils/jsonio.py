"""Helpers for JSON input/output with atomic writes and backups."""

from __future__ import annotations

import json
import os
import sys
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
    if sys.platform == "win32" and path.exists():
        path.unlink()
    tmp_path.replace(path)


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
