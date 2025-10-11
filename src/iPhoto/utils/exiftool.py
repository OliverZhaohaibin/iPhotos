"""Batch-oriented helpers for invoking the :command:`exiftool` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from ..errors import ExternalToolError


def get_metadata_batch(paths: List[Path]) -> List[Dict[str, Any]]:
    """Return metadata for *paths* by launching a single ``exiftool`` process.

    The prior implementation spawned one external process per asset which was
    both slow and prone to locale-related decoding errors on Windows.  Issuing a
    single batch request avoids that overhead and lets us explicitly request
    UTF-8 so ``exiftool`` output is decoded consistently across platforms.

    Parameters
    ----------
    paths:
        The media files that should be inspected.  Passing an empty list returns
        an empty list immediately.

    Raises
    ------
    ExternalToolError
        Raised when the ``exiftool`` executable is missing or when the command
        exits with a non-zero status code.
    """

    executable = shutil.which("exiftool")
    if executable is None:
        raise ExternalToolError(
            "exiftool executable not found. Install it from https://exiftool.org/ "
            "and ensure it is available on PATH."
        )

    if not paths:
        return []

    cmd = [
        executable,
        "-n",  # emit numeric GPS values instead of DMS strings
        "-g1",  # keep group information (e.g. Composite, GPS) in the payload
        "-json",
        "-charset",
        "UTF8",  # tell exiftool how to interpret incoming file paths
        *[str(path) for path in paths],
    ]

    try:
        # ``encoding`` forces Python to decode the JSON using UTF-8 even on
        # locales that default to a more restrictive codec such as ``cp1252``.
        # ``errors='replace'`` keeps the scan moving if unexpected byte
        # sequences appear in the metadata.
        process = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ExternalToolError(
            "exiftool executable not found. Install it from https://exiftool.org/ "
            "and ensure it is available on PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if exc.stderr else "unknown error"
        raise ExternalToolError(f"ExifTool failed with an error: {stderr}") from exc

    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalToolError(f"Failed to parse JSON output from ExifTool: {exc}") from exc


__all__ = ["get_metadata_batch"]
