"""Data models used by iPhoto."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(slots=True)
class PhotoMeta:
    rel: str
    id: str
    bytes: int
    dt: Optional[str]
    w: Optional[int]
    h: Optional[int]
    mime: Optional[str]
    make: Optional[str]
    model: Optional[str]
    gps: Optional[Dict[str, float]]
    content_id: Optional[str]


@dataclass(slots=True)
class VideoMeta:
    rel: str
    id: str
    bytes: int
    dur: Optional[float]
    mime: Optional[str]
    codec: Optional[str]
    content_id: Optional[str]
    still_image_time: Optional[float]


@dataclass(slots=True)
class LiveGroup:
    id: str
    still: str
    motion: str
    content_id: Optional[str]
    still_image_time: Optional[float]
    confidence: float
