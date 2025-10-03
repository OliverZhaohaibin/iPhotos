"""Application-wide context helpers for the GUI layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .gui.facade import AppFacade


@dataclass
class AppContext:
    """Container object shared across GUI components."""

    facade: AppFacade = field(default_factory=AppFacade)
    recent_albums: List[Path] = field(default_factory=list)

    def remember_album(self, root: Path) -> None:
        """Track *root* in the recent albums list, keeping the most recent first."""

        normalized = root.resolve()
        self.recent_albums = [entry for entry in self.recent_albums if entry != normalized]
        self.recent_albums.insert(0, normalized)
        # Keep the list short to avoid unbounded growth.
        del self.recent_albums[10:]
