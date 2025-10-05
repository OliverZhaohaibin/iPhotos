"""Helpers for loading SVG icons bundled with the desktop UI."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QIcon

_ICON_DIR = Path(__file__).resolve().parent


def icon_path(name: str) -> Path:
    """Return the absolute path to the SVG asset identified by *name*."""

    if not name:
        return _ICON_DIR / name

    filename = name if name.casefold().endswith(".svg") else f"{name}.svg"
    return _ICON_DIR / filename


@lru_cache(maxsize=None)
def load_icon(name: str) -> QIcon:
    """Return a cached :class:`~PySide6.QtGui.QIcon` for the given asset name."""

    path = icon_path(name)
    if not path.exists():
        return QIcon()
    return QIcon(str(path))


__all__ = ["icon_path", "load_icon"]

