"""Thin wrapper around the :mod:`pyexiftool` helper library."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

# ``pyexiftool`` lazily depends on the ``exiftool`` executable.  Importing it at
# runtime allows the application to present a friendlier error message when the
# optional dependency is missing from the environment.
try:  # pragma: no cover - import guard is environment dependent
    import exiftool  # type: ignore[import]
except ModuleNotFoundError:  # pragma: no cover - handled in ``get_metadata``
    exiftool = None  # type: ignore[assignment]

from ..errors import ExternalToolError


def get_metadata(path: Path) -> List[Dict[str, Any]]:
    """Return the parsed metadata for ``path`` using ExifTool.

    The :mod:`pyexiftool` binding shells out to the ``exiftool`` executable.  We
    pass the ``-n`` flag via ``common_args`` so that GPS coordinates are
    reported as plain decimal numbers instead of the default "degrees, minutes,
    seconds" string format.  This dramatically simplifies downstream parsing and
    avoids locale-dependent quirks.

    Parameters
    ----------
    path:
        Path to the media file whose metadata should be extracted.

    Raises
    ------
    ExternalToolError
        Raised when the ``exiftool`` executable is unavailable or the process
        exits with a non-zero status code.
    """

    if exiftool is None:
        raise ExternalToolError(
            "pyexiftool is installed but could not import the 'exiftool' module. "
            "Install the ExifTool executable and ensure the Python package is "
            "available."
        )

    try:
        with exiftool.ExifTool(common_args=["-n"]) as tool:
            # ``pyexiftool`` exposes ``get_metadata_batch`` for retrieving data for one or
            # more files at a time.  Some versions of the binding do not provide a
            # ``get_metadata`` convenience wrapper, so we always call the batch variant to
            # remain compatible across releases.
            return tool.get_metadata_batch([str(path)])
    except FileNotFoundError as exc:
        # ``pyexiftool`` raises ``FileNotFoundError`` if the executable cannot be
        # located.  Wrap it in ``ExternalToolError`` so callers can present a
        # consistent error surface to the GUI.
        raise ExternalToolError(
            "exiftool executable not found. Install it from "
            "https://exiftool.org/ and ensure it is available on PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "ignore") if exc.stderr else "unknown error"
        raise ExternalToolError(f"ExifTool failed for {path}: {stderr}") from exc


__all__ = ["get_metadata"]

