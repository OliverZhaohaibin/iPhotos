"""Persistent storage for album index rows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Iterator

from ..config import WORK_DIR_NAME
from .lock import FileLock
from ..errors import IndexCorruptedError
from ..utils.jsonio import atomic_write_text


class IndexStore:
    """Read/write helper for ``index.jsonl`` files."""

    def __init__(self, album_root: Path):
        self.album_root = album_root
        self.path = album_root / WORK_DIR_NAME / "index.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_rows(self, rows: Iterable[Dict[str, object]]) -> None:
        """Rewrite the entire index with *rows*."""

        payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
        if payload:
            payload += "\n"
        with FileLock(self.album_root, "index"):
            atomic_write_text(self.path, payload)

    def read_all(self) -> Iterator[Dict[str, object]]:
        """Yield all rows from the index."""

        if not self.path.exists():
            return iter(())

        def _iterator() -> Iterator[Dict[str, object]]:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise IndexCorruptedError(f"Corrupted index file: {self.path}") from exc

        return _iterator()

    def upsert_row(self, rel: str, row: Dict[str, object]) -> None:
        """Insert or update a single row identified by *rel*."""

        data = {existing["rel"]: existing for existing in self.read_all()}
        data[rel] = row
        self.write_rows(data.values())
