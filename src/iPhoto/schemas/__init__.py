"""Schema validation helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..config import SCHEMA_DIR
from ..errors import ManifestInvalidError

_ALBUM_VALIDATOR: Draft202012Validator | None = None


def _load_validator(name: str) -> Draft202012Validator:
    schema_path = SCHEMA_DIR / name
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def validate_album(document: dict[str, Any]) -> None:
    """Validate an album manifest and raise :class:`ManifestInvalidError` on failure."""

    global _ALBUM_VALIDATOR
    if _ALBUM_VALIDATOR is None:
        _ALBUM_VALIDATOR = _load_validator("album.schema.json")
    errors = sorted(_ALBUM_VALIDATOR.iter_errors(document), key=lambda err: err.path)
    if errors:
        messages = "; ".join(error.message for error in errors)
        raise ManifestInvalidError(messages)
