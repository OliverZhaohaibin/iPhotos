"""Helpers for reading Live Photo linkage metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from ....config import WORK_DIR_NAME
from ....utils.jsonio import read_json


def load_live_map(root: Path) -> Dict[str, Dict[str, object]]:
    """Return the Live Photo mapping indexed by relative path."""

    path = root / WORK_DIR_NAME / "links.json"
    if not path.exists():
        return {}
    try:
        data = read_json(path)
    except Exception:  # pragma: no cover - invalid JSON handled softly
        return {}
    mapping: Dict[str, Dict[str, object]] = {}
    for group in data.get("live_groups", []):
        gid = group.get("id")
        still = group.get("still")
        motion = group.get("motion")
        if not isinstance(gid, str):
            continue
        record: Dict[str, object] = {"id": gid, "still": still, "motion": motion}
        if isinstance(still, str) and still:
            mapping[still] = {**record, "role": "still"}
        if isinstance(motion, str) and motion:
            mapping[motion] = {**record, "role": "motion"}
    return mapping
