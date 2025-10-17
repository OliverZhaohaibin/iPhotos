"""Utility helpers for loading bundled SVG icons."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QTransform
from PySide6.QtSvg import QSvgRenderer

ICON_DIRECTORY = Path(__file__).resolve().parent / "icon"

_IconKey = Tuple[
    str,
    Optional[Tuple[int, int, int, int]],
    Optional[Tuple[int, int]],
    bool,
    Optional[float],
]
_ICON_CACHE: Dict[_IconKey, QIcon] = {}

# Pre-compiled pattern that locates ``stroke-width`` declarations inside SVG files.
_STROKE_WIDTH_RE = re.compile(r"stroke-width=([\"\'])(.*?)\1")


def load_icon(
    name: str,
    *,
    color: str | Tuple[int, int, int] | Tuple[int, int, int, int] | None = None,
    size: Tuple[int, int] | None = None,
    mirror_horizontal: bool = False,
    stroke_width: Optional[float] = None,
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
    stroke_width:
        Optional override applied to every ``stroke-width`` attribute found within the
        SVG source. When ``None`` the source asset is rendered without modification.
    """

    normalized_color = _normalize_color_key(color)
    cache_key: _IconKey = (
        name,
        normalized_color,
        tuple(size) if size else None,
        mirror_horizontal,
        stroke_width,
    )
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]

    path = ICON_DIRECTORY / name
    if not path.exists():  # pragma: no cover - defensive guard
        raise FileNotFoundError(f"Icon '{name}' not found in {ICON_DIRECTORY}")

    svg_data: Optional[QByteArray] = None
    if stroke_width is not None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            # Fallback to the unmodified asset when the SVG cannot be read.
            svg_data = None
        else:
            # Replace any ``stroke-width`` attributes before the renderer loads the SVG.
            modified = _STROKE_WIDTH_RE.sub(f'stroke-width="{stroke_width}"', content)
            svg_data = QByteArray(modified.encode("utf-8"))

    if (
        svg_data is None
        and stroke_width is None
        and normalized_color is None
        and size is None
        and not mirror_horizontal
    ):
        icon = QIcon(str(path))
        _ICON_CACHE[cache_key] = icon
        return icon

    renderer = QSvgRenderer()
    if svg_data is not None:
        renderer.load(svg_data)
    else:
        renderer.load(str(path))
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
        tinted.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tinted)
        painter.fillRect(tinted.rect(), tint)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        painter.drawPixmap(0, 0, pixmap)
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

