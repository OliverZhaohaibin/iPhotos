"""Settings file management with validation and change notifications."""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..errors import SettingsLoadError, SettingsValidationError
from ..utils.jsonio import read_json, write_json
from .schema import DEFAULT_SETTINGS, merge_with_defaults


def default_settings_path() -> Path:
    """Return the default settings.json location for the current platform."""

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "iPhoto" / "settings.json"
        return Path.home() / "AppData" / "Roaming" / "iPhoto" / "settings.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "iPhoto" / "settings.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "iPhoto" / "settings.json"
    return Path.home() / ".config" / "iPhoto" / "settings.json"


class SettingsManager(QObject):
    """Load, validate and persist user settings for the application."""

    settingsChanged = Signal(str, object)

    def __init__(self, path: Path | None = None) -> None:
        super().__init__()
        self._path = path
        self._data: dict[str, Any] = deepcopy(DEFAULT_SETTINGS)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Load the settings JSON from disk, creating defaults if missing."""

        path = self._path or default_settings_path()
        self._path = path
        if path.exists():
            try:
                payload = read_json(path)
            except Exception as exc:  # pragma: no cover - defensive guard
                raise SettingsLoadError(str(exc)) from exc
        else:
            payload = None
        try:
            self._data = merge_with_defaults(payload)
        except Exception as exc:  # pragma: no cover - defensive guard
            raise SettingsValidationError(str(exc)) from exc
        self._write()

    def get(self, key: str, default: Any | None = None) -> Any:
        """Return the value for *key*, supporting dotted access for nested keys."""

        target = self._data
        parts = key.split(".")
        for index, part in enumerate(parts):
            if not isinstance(target, dict) or part not in target:
                return default
            value = target[part]
            if index == len(parts) - 1:
                return value
            target = value
        return default

    def set(self, key: str, value: Any) -> None:
        """Update *key* with *value* and persist the change."""

        parts = key.split(".")
        target: dict[str, Any] = self._data
        for part in parts[:-1]:
            branch = target.get(part)
            if not isinstance(branch, dict):
                branch = {}
                target[part] = branch
            target = branch
        final_key = parts[-1]
        if isinstance(value, Path):
            value = str(value)
        target[final_key] = value
        try:
            self._data = merge_with_defaults(self._data)
        except Exception as exc:  # pragma: no cover - defensive guard
            raise SettingsValidationError(str(exc)) from exc
        self._write()
        self.settingsChanged.emit(key, value)

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------
    def _write(self) -> None:
        path = self._path or default_settings_path()
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, self._data)


__all__ = ["SettingsManager", "default_settings_path"]
