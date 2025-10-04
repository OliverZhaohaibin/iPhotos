"""Schema helpers for the application settings file."""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator

SETTINGS_SCHEMA: dict[str, Any] = {
    "$id": "iPhoto/settings.schema.json",
    "type": "object",
    "required": ["schema", "ui", "last_open_albums"],
    "properties": {
        "schema": {"const": "iPhoto/settings@1"},
        "basic_library_path": {"type": ["string", "null"]},
        "ui": {
            "type": "object",
            "properties": {
                "theme": {"type": "string"},
                "sidebar_width": {"type": "number", "minimum": 120},
            },
            "additionalProperties": True,
        },
        "last_open_albums": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "additionalProperties": True,
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema": "iPhoto/settings@1",
    "basic_library_path": None,
    "ui": {"theme": "light", "sidebar_width": 280},
    "last_open_albums": [],
}

_validator = Draft202012Validator(SETTINGS_SCHEMA)


def _normalise_last_open(entries: list[Any]) -> list[str]:
    normalised: list[str] = []
    for entry in entries:
        try:
            path = os.fspath(entry)
        except TypeError:
            continue
        normalised.append(str(path))
    return normalised


def merge_with_defaults(data: dict[str, Any] | None) -> dict[str, Any]:
    """Merge *data* with :data:`DEFAULT_SETTINGS` and validate the result."""

    merged = deepcopy(DEFAULT_SETTINGS)
    if data:
        for key, value in data.items():
            if key == "ui" and isinstance(value, dict):
                target = merged.setdefault("ui", {})
                for sub_key, sub_value in value.items():
                    target[sub_key] = sub_value
                continue
            if key == "last_open_albums" and isinstance(value, list):
                merged[key] = _normalise_last_open(value)
                continue
            if key == "basic_library_path" and value not in {None, ""}:
                try:
                    merged[key] = os.fspath(value)
                except TypeError:
                    continue
                continue
            merged[key] = value
    _validator.validate(merged)
    return merged


def validate_settings(data: dict[str, Any]) -> None:
    """Validate *data* against the settings schema."""

    _validator.validate(data)


__all__ = ["DEFAULT_SETTINGS", "SETTINGS_SCHEMA", "merge_with_defaults", "validate_settings"]
