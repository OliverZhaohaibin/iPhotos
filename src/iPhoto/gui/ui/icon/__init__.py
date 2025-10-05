"""Helpers for loading SVG icons bundled with the desktop UI."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QIcon

_ICON_DIR = Path(__file__).resolve().parent


def icon_path(name: str) -> Path:
    """Return the absolute path to the SVG asset identified by *name*."""

    candidate = _ICON_DIR / name
    if candidate.suffix:
        return candidate
    return candidate.with_suffix(".svg")


@lru_cache(maxsize=None)
def load_icon(name: str) -> QIcon:
    """Return a cached :class:`~PySide6.QtGui.QIcon` for the given asset name."""

    path = icon_path(name)
    if not path.exists():
        return QIcon()
    return QIcon(str(path))


__all__ = ["icon_path", "load_icon"]

