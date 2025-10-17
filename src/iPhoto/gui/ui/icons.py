"""Utility helpers for loading bundled SVG icons."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QTransform
from PySide6.QtSvg import QSvgRenderer

ICON_DIRECTORY = Path(__file__).resolve().parent / "icon"

_IconKey = Tuple[str, Optional[Tuple[int, int, int, int]], Optional[Tuple[int, int]], bool]
_ICON_CACHE: Dict[_IconKey, QIcon] = {}


def load_icon(
    name: str,
    *,
    color: str | Tuple[int, int, int] | Tuple[int, int, int, int] | None = None,
    size: Tuple[int, int] | None = None,
    mirror_horizontal: bool = False,
) -> QIcon:
    """Return a :class:`QIcon` for *name* from the bundled icon directory.

    Parameters
    ----------
    name:
        File name (including the ``.svg`` suffix) of the icon to load.
    color:
        Optional colour tint applied to the icon. Accepts hex strings (``"#RRGGBB"``)
        or tuples representing RGB/RGBA components. When omitted, the original
        colours from the SVG asset are preserved.
    size:
        Optional target size (width, height) used when rendering the SVG. When not
        supplied, the intrinsic size declared in the SVG is used.
    mirror_horizontal:
        When ``True`` the resulting pixmap is mirrored horizontally. This is useful
        for reusing directional icons (e.g. play/previous).
    """

    normalized_color = _normalize_color_key(color)
    cache_key: _IconKey = (name, normalized_color, tuple(size) if size else None, mirror_horizontal)
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]

    path = ICON_DIRECTORY / name
    if not path.exists():  # pragma: no cover - defensive guard
        raise FileNotFoundError(f"Icon '{name}' not found in {ICON_DIRECTORY}")

    if normalized_color is None and size is None and not mirror_horizontal:
        icon = QIcon(str(path))
        _ICON_CACHE[cache_key] = icon
        return icon

    renderer = QSvgRenderer(str(path))
    target_size = QSize(*size) if size else renderer.defaultSize()
    if not target_size.isValid():
        target_size = QSize(64, 64)

    pixmap = QPixmap(target_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()

    if normalized_color is not None:
        tint = QColor.fromRgb(*normalized_color)
        tinted = QPixmap(pixmap.size())
        tinted.setDevicePixelRatio(pixmap.devicePixelRatio())
        tinted.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tinted)
        try:
            # Painting the original glyph first preserves its alpha mask. The
            # SourceIn composition that follows then injects the requested tint
            # while keeping crisp edges even for supersampled pixmaps.
            painter.drawPixmap(0, 0, pixmap)
            painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
            painter.fillRect(tinted.rect(), tint)
        finally:
            painter.end()
        pixmap = tinted

    if mirror_horizontal:
        transform = QTransform()
        transform.scale(-1, 1)
        pixmap = pixmap.transformed(transform, Qt.TransformationMode.SmoothTransformation)

    icon = QIcon()
    icon.addPixmap(pixmap)
    _ICON_CACHE[cache_key] = icon
    return icon


def _normalize_color_key(
    color: str | Tuple[int, int, int] | Tuple[int, int, int, int] | None
) -> Tuple[int, int, int, int] | None:
    if color is None:
        return None
    qcolor = QColor()
    if isinstance(color, str):
        qcolor = QColor(color)
    elif isinstance(color, tuple):
        if len(color) == 3:
            qcolor = QColor(color[0], color[1], color[2])
        elif len(color) == 4:
            qcolor = QColor(color[0], color[1], color[2], color[3])
        else:  # pragma: no cover - defensive guard
            raise ValueError("Colour tuples must be RGB or RGBA")
    else:  # pragma: no cover - defensive guard
        raise TypeError("Colour must be a hex string or RGB/RGBA tuple")
    if not qcolor.isValid():  # pragma: no cover - defensive guard
        raise ValueError(f"Invalid colour specification: {color!r}")
    return (qcolor.red(), qcolor.green(), qcolor.blue(), qcolor.alpha())


__all__ = ["load_icon"]

