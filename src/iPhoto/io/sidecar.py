"""Read/write helpers for ``.ipo`` XML sidecar files."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping
import xml.etree.ElementTree as ET

from ..core.image_filters import LIGHT_KEYS

_SIDE_CAR_ROOT = "iPhotoAdjustments"
_LIGHT_NODE = "Light"
_VERSION_ATTR = "version"
_CURRENT_VERSION = "1.0"


def sidecar_path_for_asset(asset_path: Path) -> Path:
    """Return the expected sidecar path for *asset_path*."""

    return asset_path.with_suffix(".ipo")


def load_adjustments(asset_path: Path) -> Dict[str, float]:
    """Return light adjustments stored alongside *asset_path*.

    Missing files or parsing errors are treated as an empty adjustment set so the
    caller can continue working with the unmodified image.  Individual entries
    that fail to parse fall back to ``0.0`` rather than aborting the load, which
    keeps the feature resilient against manual edits or older file formats.
    """

    sidecar_path = sidecar_path_for_asset(asset_path)
    if not sidecar_path.exists():
        return {}

    try:
        tree = ET.parse(sidecar_path)
    except ET.ParseError:
        return {}
    root = tree.getroot()
    if root.tag != _SIDE_CAR_ROOT:
        return {}

    light_node = root.find(_LIGHT_NODE)
    if light_node is None:
        return {}

    result: Dict[str, float] = {}
    for key in LIGHT_KEYS:
        element = light_node.find(key)
        if element is None or element.text is None:
            continue
        try:
            result[key] = float(element.text.strip())
        except ValueError:
            continue
    return result


def save_adjustments(asset_path: Path, adjustments: Mapping[str, float]) -> Path:
    """Persist *adjustments* next to *asset_path* and return the sidecar path."""

    sidecar_path = sidecar_path_for_asset(asset_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)

    root = ET.Element(_SIDE_CAR_ROOT)
    root.set(_VERSION_ATTR, _CURRENT_VERSION)
    light = ET.SubElement(root, _LIGHT_NODE)
    for key in LIGHT_KEYS:
        value = float(adjustments.get(key, 0.0))
        child = ET.SubElement(light, key)
        child.text = f"{value:.2f}"

    tmp_path = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
    tree = ET.ElementTree(root)
    tree.write(tmp_path, encoding="utf-8", xml_declaration=True)

    try:
        tmp_path.replace(sidecar_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    return sidecar_path
