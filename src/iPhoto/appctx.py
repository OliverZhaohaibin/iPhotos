"""Application-wide context helpers for the GUI layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - only for type checking
    from .gui.facade import AppFacade
    from .library.manager import LibraryManager
    from .settings.manager import SettingsManager


def _create_facade() -> "AppFacade":
    """Factory that imports :class:`AppFacade` lazily to avoid circular imports."""

    from .gui.facade import AppFacade  # Local import prevents circular dependency

    return AppFacade()


def _create_settings_manager():
    from .settings.manager import SettingsManager

    manager = SettingsManager()
    manager.load()
    return manager


def _create_library_manager():
    from .library.manager import LibraryManager

    return LibraryManager()


@dataclass
class AppContext:
    """Container object shared across GUI components."""

    settings: "SettingsManager" = field(default_factory=_create_settings_manager)
    library: "LibraryManager" = field(default_factory=_create_library_manager)
    facade: "AppFacade" = field(default_factory=_create_facade)
    recent_albums: List[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        from .errors import LibraryError

        # ``AppFacade`` needs to observe the shared library manager so that
        # manifest writes performed while browsing nested albums can keep the
        # global "Favorites" collection in sync.  The binding is established
        # eagerly here because both collaborators are constructed via default
        # factories before ``__post_init__`` runs.
        self.facade.bind_library(self.library)

        basic_path = self.settings.get("basic_library_path")
        if isinstance(basic_path, str) and basic_path:
            candidate = Path(basic_path).expanduser()
            if candidate.exists():
                try:
                    self.library.bind_path(candidate)
                except LibraryError as exc:
                    self.library.errorRaised.emit(str(exc))
            else:
                self.library.errorRaised.emit(
                    f"Basic Library path is unavailable: {candidate}"
                )
        stored = self.settings.get("last_open_albums", []) or []
        resolved: list[Path] = []
        for entry in stored:
            try:
                resolved.append(Path(entry))
            except TypeError:
                continue
        if resolved:
            self.recent_albums = resolved[:10]

    def remember_album(self, root: Path) -> None:
        """Track *root* in the recent albums list, keeping the most recent first."""

        normalized = root.resolve()
        self.recent_albums = [entry for entry in self.recent_albums if entry != normalized]
        self.recent_albums.insert(0, normalized)
        # Keep the list short to avoid unbounded growth.
        del self.recent_albums[10:]
        self.settings.set(
            "last_open_albums",
            [str(path) for path in self.recent_albums],
        )
