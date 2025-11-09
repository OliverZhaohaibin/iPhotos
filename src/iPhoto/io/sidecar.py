"""Read/write helpers for ``.ipo`` XML sidecar files."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping
import xml.etree.ElementTree as ET

from ..core.light_resolver import LIGHT_KEYS, resolve_light_vector
from ..core.color_resolver import COLOR_KEYS, ColorResolver, ColorStats

BW_KEYS = (
    "BW_Master",
    "BW_Intensity",
    "BW_Neutrals",
    "BW_Tone",
    "BW_Grain",
)

BW_DEFAULTS = {
    "BW_Master": 0.5,
    "BW_Intensity": 0.5,
    "BW_Neutrals": 0.0,
    "BW_Tone": 0.0,
    "BW_Grain": 0.0,
}

_BW_RANGE_KEYS = {"BW_Master", "BW_Intensity", "BW_Neutrals", "BW_Tone"}


def _normalise_bw_value(key: str, value: float) -> float:
    """Return *value* mapped to the modern ``[0, 1]`` range for B&W controls."""

    numeric = float(value)
    if key in _BW_RANGE_KEYS and (numeric < 0.0 or numeric > 1.0):
        numeric = (numeric + 1.0) * 0.5
    return max(0.0, min(1.0, numeric))

_SIDE_CAR_ROOT = "iPhotoAdjustments"
_LIGHT_NODE = "Light"
_VERSION_ATTR = "version"
_CURRENT_VERSION = "1.0"


def sidecar_path_for_asset(asset_path: Path) -> Path:
    """Return the expected sidecar path for *asset_path*."""

    return asset_path.with_suffix(".ipo")


def load_adjustments(asset_path: Path) -> Dict[str, float | bool]:
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

    result: Dict[str, float | bool] = {}
    master_element = light_node.find("Light_Master")
    if master_element is not None and master_element.text is not None:
        try:
            result["Light_Master"] = float(master_element.text.strip())
        except ValueError:
            result["Light_Master"] = 0.0
    enabled_element = light_node.find("Light_Enabled")
    if enabled_element is not None and enabled_element.text is not None:
        text = enabled_element.text.strip().lower()
        result["Light_Enabled"] = text in {"1", "true", "yes", "on"}
    else:
        result["Light_Enabled"] = True
    for key in LIGHT_KEYS:
        element = light_node.find(key)
        if element is None or element.text is None:
            continue
        try:
            result[key] = float(element.text.strip())
        except ValueError:
            continue
    color_master = light_node.find("Color_Master")
    if color_master is not None and color_master.text is not None:
        try:
            result["Color_Master"] = float(color_master.text.strip())
        except ValueError:
            result["Color_Master"] = 0.0
    color_enabled = light_node.find("Color_Enabled")
    if color_enabled is not None and color_enabled.text is not None:
        text = color_enabled.text.strip().lower()
        result["Color_Enabled"] = text in {"1", "true", "yes", "on"}
    else:
        result["Color_Enabled"] = True
    for key in COLOR_KEYS:
        element = light_node.find(key)
        if element is None or element.text is None:
            continue
        try:
            result[key] = float(element.text.strip())
        except ValueError:
            continue
    bw_enabled = light_node.find("BW_Enabled")
    if bw_enabled is not None and bw_enabled.text is not None:
        text = bw_enabled.text.strip().lower()
        result["BW_Enabled"] = text in {"1", "true", "yes", "on"}
    else:
        result["BW_Enabled"] = False

    for key in BW_KEYS:
        element = light_node.find(key)
        if element is None or element.text is None:
            continue
        try:
            result[key] = float(element.text.strip())
        except ValueError:
            continue

    return result


def save_adjustments(asset_path: Path, adjustments: Mapping[str, float | bool]) -> Path:
    """Persist *adjustments* next to *asset_path* and return the sidecar path."""

    sidecar_path = sidecar_path_for_asset(asset_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)

    root = ET.Element(_SIDE_CAR_ROOT)
    root.set(_VERSION_ATTR, _CURRENT_VERSION)
    light = ET.SubElement(root, _LIGHT_NODE)
    master_element = ET.SubElement(light, "Light_Master")
    master_value = float(adjustments.get("Light_Master", 0.0))
    master_element.text = f"{master_value:.2f}"

    enabled_element = ET.SubElement(light, "Light_Enabled")
    enabled = bool(adjustments.get("Light_Enabled", True))
    enabled_element.text = "true" if enabled else "false"

    for key in LIGHT_KEYS:
        value = float(adjustments.get(key, 0.0))
        child = ET.SubElement(light, key)
        child.text = f"{value:.2f}"

    color_master = ET.SubElement(light, "Color_Master")
    color_master_value = float(adjustments.get("Color_Master", 0.0))
    color_master.text = f"{color_master_value:.2f}"

    color_enabled = ET.SubElement(light, "Color_Enabled")
    color_enabled_value = bool(adjustments.get("Color_Enabled", True))
    color_enabled.text = "true" if color_enabled_value else "false"

    for key in COLOR_KEYS:
        value = float(adjustments.get(key, 0.0))
        child = ET.SubElement(light, key)
        child.text = f"{value:.2f}"

    bw_enabled = ET.SubElement(light, "BW_Enabled")
    bw_enabled_value = bool(adjustments.get("BW_Enabled", False))
    bw_enabled.text = "true" if bw_enabled_value else "false"

    for key in BW_KEYS:
        value = _normalise_bw_value(key, float(adjustments.get(key, BW_DEFAULTS.get(key, 0.0))))
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


def resolve_render_adjustments(
    adjustments: Mapping[str, float | bool] | None,
    *,
    color_stats: ColorStats | None = None,
) -> Dict[str, float]:
    """Return Light adjustments suitable for rendering pipelines.

    ``load_adjustments`` exposes the raw session values, which now contain the master slider
    (`Light_Master`) and enable toggle (`Light_Enabled`) alongside the seven per-control deltas.
    Rendering helpers expect the final per-slider values rather than the stored deltas, so the
    helpers outside the edit session must resolve the vector before handing it to
    :func:`apply_adjustments`.
    """

    if not adjustments:
        return {}

    try:
        master_value = float(adjustments.get("Light_Master", 0.0))
    except (TypeError, ValueError):
        master_value = 0.0

    light_enabled = bool(adjustments.get("Light_Enabled", True))

    resolved: Dict[str, float] = {}
    overrides: Dict[str, float] = {}
    color_overrides: Dict[str, float] = {}
    for key, value in adjustments.items():
        if key in ("Light_Master", "Light_Enabled"):
            continue
        if value is None:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if key in LIGHT_KEYS:
            overrides[key] = numeric_value
        elif key in COLOR_KEYS:
            color_overrides[key] = numeric_value
        elif key == "BW_Master":
            continue
        else:
            resolved[key] = numeric_value

    if light_enabled:
        light_values = resolve_light_vector(master_value, overrides, mode="delta")
    else:
        light_values = {key: 0.0 for key in LIGHT_KEYS}
    resolved.update(light_values)
    stats = color_stats or ColorStats()
    color_master = float(adjustments.get("Color_Master", 0.0))
    color_enabled = bool(adjustments.get("Color_Enabled", True))
    if color_enabled:
        color_values = ColorResolver.resolve_color_vector(
            color_master,
            color_overrides,
            stats=stats,
            mode="delta",
        )
    else:
        color_values = {key: 0.0 for key in COLOR_KEYS}
    gain_r, gain_g, gain_b = stats.white_balance_gain
    resolved.update(color_values)
    resolved["Color_Gain_R"] = gain_r
    resolved["Color_Gain_G"] = gain_g
    resolved["Color_Gain_B"] = gain_b

    bw_enabled = bool(adjustments.get("BW_Enabled", False))
    if bw_enabled:
        resolved["BWIntensity"] = _normalise_bw_value("BW_Intensity", float(adjustments.get("BW_Intensity", 0.5)))
        resolved["BWNeutrals"] = _normalise_bw_value("BW_Neutrals", float(adjustments.get("BW_Neutrals", 0.0)))
        resolved["BWTone"] = _normalise_bw_value("BW_Tone", float(adjustments.get("BW_Tone", 0.0)))
        resolved["BWGrain"] = _normalise_bw_value("BW_Grain", float(adjustments.get("BW_Grain", 0.0)))
    else:
        resolved["BWIntensity"] = 0.5
        resolved["BWNeutrals"] = 0.0
        resolved["BWTone"] = 0.0
        resolved["BWGrain"] = 0.0

    return resolved
