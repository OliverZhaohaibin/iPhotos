"""Tree node structures for the basic library."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class AlbumNode:
    """Represents an album or sub-album within the basic library."""

    path: Path
    level: int
    title: str
    has_manifest: bool

    def is_top_level(self) -> bool:
        """Return ``True`` when the node resides directly under the library root."""

        return self.level == 1


__all__ = ["AlbumNode"]
