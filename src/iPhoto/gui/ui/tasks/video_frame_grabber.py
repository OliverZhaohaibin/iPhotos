"""Utilities for extracting representative video frames for thumbnails."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from ....config import THUMBNAIL_SEEK_GUARD_SEC
from ....errors import ExternalToolError
from ....utils.ffmpeg import extract_video_frame
from ...utils import image_loader


def grab_video_frame(
    path: Path,
    size: QSize,
    *,
    still_image_time: Optional[float] = None,
    duration: Optional[float] = None,
) -> Optional[QImage]:
    """Return a decoded frame for *path* scaled to *size*."""

    frame_data: Optional[bytes] = None
    for target in _seek_targets(still_image_time, duration):
        try:
            frame_data = extract_video_frame(
                path,
                at=target,
                scale=(max(size.width(), 1), max(size.height(), 1)),
                format="jpeg",
            )
        except ExternalToolError:
            frame_data = None
            continue
        if frame_data:
            break
    if not frame_data:
        return None
    return image_loader.qimage_from_bytes(frame_data)


def _seek_targets(
    still_image_time: Optional[float], duration: Optional[float]
) -> Iterable[Optional[float]]:
    targets: List[Optional[float]] = []
    seen: set[Optional[float]] = set()

    def add(candidate: Optional[float]) -> None:
        if candidate is None:
            key: Optional[float] = None
            value: Optional[float] = None
        else:
            value = _normalize_seek(candidate, duration)
            key = value
        if key in seen:
            return
        seen.add(key)
        targets.append(value)

    if still_image_time is not None:
        add(still_image_time)
    elif duration is not None and duration > 0:
        add(duration / 2.0)
    add(None)
    return targets


def _normalize_seek(value: float, duration: Optional[float]) -> float:
    normalized = max(value, 0.0)
    if duration and duration > 0:
        guard = min(
            max(THUMBNAIL_SEEK_GUARD_SEC, duration * 0.1),
            duration / 2.0,
        )
        max_seek = max(duration - guard, 0.0)
        if normalized > max_seek:
            normalized = max_seek
    return normalized
