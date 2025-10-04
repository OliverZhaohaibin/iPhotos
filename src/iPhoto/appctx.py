"""Application-wide context helpers for the GUI layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - only for type checking
    from .gui.facade import AppFacade


def _create_facade() -> "AppFacade":
    """Factory that imports :class:`AppFacade` lazily to avoid circular imports."""

    from .gui.facade import AppFacade  # Local import prevents circular dependency

    return AppFacade()


@dataclass
class AppContext:
    """Container object shared across GUI components."""

    facade: "AppFacade" = field(default_factory=_create_facade)
    recent_albums: List[Path] = field(default_factory=list)
    library_root: Path | None = None

    def remember_album(self, root: Path) -> None:
        """Track *root* in the recent albums list, keeping the most recent first."""

        normalized = root.resolve()
        self.recent_albums = [entry for entry in self.recent_albums if entry != normalized]
        self.recent_albums.insert(0, normalized)
        # Keep the list short to avoid unbounded growth.
        del self.recent_albums[10:]

    def set_library_root(self, root: Path | None) -> None:
        """Update the active library root directory."""

        self.library_root = root.resolve() if root is not None else None
