"""Helpers for loading SVG icons bundled with the desktop UI."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Tuple

from PySide6.QtGui import QIcon

from ..icons import load_icon as _load_icon_with_options

_ICON_DIR = Path(__file__).resolve().parent


def icon_path(name: str) -> Path:
    """Return the absolute path to the SVG asset identified by *name*."""

    if not name:
        return _ICON_DIR / name

    filename = name if name.casefold().endswith(".svg") else f"{name}.svg"
    return _ICON_DIR / filename


@lru_cache(maxsize=None)
def load_icon(
    name: str,
    *,
    color: str | Tuple[int, int, int] | Tuple[int, int, int, int] | None = None,
    size: Tuple[int, int] | None = None,
    mirror_horizontal: bool = False,
) -> QIcon:
    """Return a cached :class:`~PySide6.QtGui.QIcon` for the given asset name.

    The adapter keeps backwards compatibility with legacy call sites that omit
    the ``.svg`` suffix while still exposing the richer tinting and scaling
    options implemented in :mod:`iPhoto.gui.ui.icons`. ``lru_cache`` is retained
    so repeated calls remain cheap even when recolouring is requested.
    """

    filename = name if name.casefold().endswith(".svg") else f"{name}.svg"
    return _load_icon_with_options(
        filename,
        color=color,
        size=size,
        mirror_horizontal=mirror_horizontal,
    )


__all__ = ["icon_path", "load_icon"]

