"""Compatible wrapper around the various :mod:`pyexiftool` helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import ExternalToolError

# ``pyexiftool`` has shipped multiple helper classes across releases.  Newer
# versions expose :class:`ExifToolHelper` while older builds only provide the
# lower-level :class:`ExifTool`.  Importing conditionally lets us support both
# variants while also keeping the module importable on systems where the Python
# package is missing entirely.
try:  # pragma: no cover - depends on optional third-party package
    from exiftool import ExifToolHelper, ExifTool
except Exception:  # pragma: no cover - best effort compatibility shim
    ExifToolHelper = None  # type: ignore[assignment]
    try:  # pragma: no cover - depends on optional third-party package
        from exiftool import ExifTool
    except Exception:  # pragma: no cover - best effort compatibility shim
        ExifTool = None  # type: ignore[assignment]


def _call_exiftool_subprocess(paths: List[str]) -> List[Dict[str, Any]]:
    """Invoke the ``exiftool`` CLI directly as a last-resort fallback.

    The command mirrors the arguments we pass through the Python bindings so the
    downstream parsing code receives consistent, numeric GPS coordinates and a
    compact key layout.
    """

    cmd = [
        "exiftool",
        "-n",  # return numeric values for GPS fields instead of DMS strings
        "-api",
        "compact=1",  # collapse duplicate keys and avoid nested structures
        "-g1",  # include group names for keys (matches helper behaviour)
        "-json",
        *paths,
    ]
    try:
        output = subprocess.check_output(cmd, text=True)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - rare error path
        stderr = exc.stderr if exc.stderr else "unknown error"
        raise ExternalToolError(f"ExifTool failed: {stderr}") from exc
    return json.loads(output)


def get_metadata(path: str | Path) -> Optional[Dict[str, Any]]:
    """Return metadata for ``path`` using whichever helper is available.

    The function prefers :class:`ExifToolHelper` because it offers the most
    user-friendly API on modern ``pyexiftool`` releases.  When that helper is not
    present we gracefully fall back to the legacy :class:`ExifTool` wrapper and
    finally to executing the ``exiftool`` binary directly.  All code paths ensure
    the ``-n`` flag is supplied so GPS coordinates arrive as decimals, which
    keeps the rest of the application logic straightforward.
    """

    executable = shutil.which("exiftool")
    if executable is None:
        raise ExternalToolError(
            "exiftool executable not found. Install it from https://exiftool.org/ "
            "and ensure it is available on PATH."
        )

    target = str(path)

    if ExifToolHelper is not None:
        try:
            with ExifToolHelper(common_args=["-n", "-api", "compact=1", "-g1"]) as helper:
                payload = helper.get_metadata([target])
        except FileNotFoundError as exc:  # pragma: no cover - depends on runtime env
            raise ExternalToolError(
                "pyexiftool could not locate the exiftool executable."
            ) from exc
        return payload[0] if payload else None

    if ExifTool is not None:
        try:
            with ExifTool(common_args=["-n", "-api", "compact=1", "-g1"]) as tool:
                if hasattr(tool, "get_metadata_batch"):
                    payload = tool.get_metadata_batch([target])
                    return payload[0] if payload else None
                if hasattr(tool, "get_metadata"):
                    payload = tool.get_metadata(target)
                    if isinstance(payload, list):
                        return payload[0] if payload else None
                    return payload
        except FileNotFoundError as exc:  # pragma: no cover - depends on runtime env
            raise ExternalToolError(
                "pyexiftool could not locate the exiftool executable."
            ) from exc

    payload = _call_exiftool_subprocess([target])
    return payload[0] if payload else None


__all__ = ["get_metadata"]

