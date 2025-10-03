"""Hashing utilities."""

from __future__ import annotations

from pathlib import Path

import xxhash


def file_xxh3(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the XXH3 128-bit hash of *path*."""

    hasher = xxhash.xxh3_128()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()
