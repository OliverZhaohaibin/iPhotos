"""Lightweight wrappers around the ``ffmpeg`` toolchain."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..errors import ExternalToolError

_FFMPEG_LOG_LEVEL = "error"


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Execute *command* and return the completed process."""

    try:
        process = subprocess.run(
            list(command),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # pragma: no cover - depends on environment
        raise ExternalToolError("ffmpeg executable not found on PATH") from exc
    return process


def extract_video_frame(
    source: Path,
    *,
    at: Optional[float] = None,
    scale: Optional[tuple[int, int]] = None,
) -> bytes:
    """Return a PNG frame extracted from *source*.

    Parameters
    ----------
    source:
        Path to the input video file.
    at:
        Timestamp in seconds to sample. When ``None`` the first frame is used.
    scale:
        Optional ``(width, height)`` hint used to scale the output frame while
        preserving aspect ratio.
    """

    command: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        _FFMPEG_LOG_LEVEL,
        "-nostdin",
        "-y",
    ]
    if at is not None:
        command += ["-ss", f"{max(at, 0):.3f}"]
    command += [
        "-i",
        str(source),
        "-an",
        "-frames:v",
        "1",
    ]
    filters: list[str] = []
    if scale is not None:
        width, height = scale
        if width > 0 and height > 0:
            # Avoid single-quoted filter expressions because ``ffmpeg`` on Windows does not
            # interpret them the same way as Unix shells. Passing the raw expression keeps the
            # command portable across platforms when using ``subprocess`` with ``shell=False``.
            filters.append(
                "scale=min({w},iw):min({h},ih):force_original_aspect_ratio=decrease".format(
                    w=width,
                    h=height,
                )
            )
    filters.append("format=rgba")
    if filters:
        command += ["-vf", ",".join(filters)]
    command += ["-f", "image2", "-vcodec", "png"]

    fd, tmp_name = tempfile.mkstemp(suffix=".png")
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        command.append(str(tmp_path))
        process = _run_command(command)
        if process.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            stderr = process.stderr.decode("utf-8", "ignore").strip()
            raise ExternalToolError(
                f"ffmpeg failed to extract frame from {source}: {stderr or 'unknown error'}"
            )
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


def probe_media(source: Path) -> Dict[str, Any]:
    """Return ffprobe metadata for *source*.

    The JSON structure mirrors ffprobe's ``show_format`` and ``show_streams``
    output. ``ExternalToolError`` is raised when the toolchain is unavailable or
    returns an error.
    """

    command = [
        "ffprobe",
        "-hide_banner",
        "-loglevel",
        _FFMPEG_LOG_LEVEL,
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]

    process = _run_command(command)
    if process.returncode != 0 or not process.stdout:
        stderr = process.stderr.decode("utf-8", "ignore").strip()
        raise ExternalToolError(
            f"ffprobe failed to inspect {source}: {stderr or 'unknown error'}"
        )
    try:
        return json.loads(process.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ExternalToolError("ffprobe returned invalid JSON output") from exc
