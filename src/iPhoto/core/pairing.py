"""Live Photo pairing logic."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from dateutil import parser

from ..config import LIVE_DURATION_PREFERRED, PAIR_TIME_DELTA_SEC
from ..models.types import LiveGroup


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parser.isoparse(value)
    except (ValueError, TypeError):
        return None


def pair_live(index_rows: List[Dict[str, object]]) -> List[LiveGroup]:
    """Pair still and motion assets into :class:`LiveGroup` objects."""

    photos: Dict[str, Dict[str, object]] = {}
    videos: Dict[str, Dict[str, object]] = {}
    for row in index_rows:
        mime = (row.get("mime") or "").lower()
        if mime.startswith("image/"):
            photos[row["rel"]] = row
        elif mime.startswith("video/"):
            videos[row["rel"]] = row

    matched: Dict[str, LiveGroup] = {}
    used_videos: set[str] = set()

    # 1) strong match by content_id
    video_by_cid: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for video in videos.values():
        cid = video.get("content_id")
        if cid:
            video_by_cid[cid].append(video)
    for photo in photos.values():
        cid = photo.get("content_id")
        if not cid or cid not in video_by_cid:
            continue
        chosen = _select_best_video(video_by_cid[cid])
        if chosen:
            matched[photo["rel"]] = LiveGroup(
                id=f"live_{hash((photo['rel'], chosen['rel'])) & 0xFFFFFF:x}",
                still=photo["rel"],
                motion=chosen["rel"],
                content_id=cid,
                still_image_time=chosen.get("still_image_time"),
                confidence=1.0,
            )
            used_videos.add(chosen["rel"])

    # 2) medium match by same stem + time delta
    for photo in photos.values():
        if photo["rel"] in matched:
            continue
        stem = Path(photo["rel"]).stem
        candidates = [v for v in videos.values() if Path(v["rel"]).stem == stem]
        chosen = _match_by_time(photo, candidates, used_videos)
        if chosen:
            used_videos.add(chosen["rel"])
            matched[photo["rel"]] = _build_group(photo, chosen, confidence=0.7)

    # 3) weak match by directory proximity
    for photo in photos.values():
        if photo["rel"] in matched:
            continue
        folder = str(Path(photo["rel"]).parent)
        candidates = [v for v in videos.values() if str(Path(v["rel"]).parent) == folder]
        chosen = _match_by_time(photo, candidates, used_videos)
        if chosen:
            used_videos.add(chosen["rel"])
            matched[photo["rel"]] = _build_group(photo, chosen, confidence=0.5)

    return list(matched.values())


def _match_by_time(
    photo: Dict[str, object],
    candidates: Iterable[Dict[str, object]],
    used_videos: set[str],
) -> Dict[str, object] | None:
    photo_dt = _parse_dt(photo.get("dt"))
    best: Tuple[float, Dict[str, object]] | None = None
    for candidate in candidates:
        if candidate["rel"] in used_videos:
            continue
        video_dt = _parse_dt(candidate.get("dt"))
        if not photo_dt or not video_dt:
            continue
        delta = abs((photo_dt - video_dt).total_seconds())
        if delta > PAIR_TIME_DELTA_SEC:
            continue
        if best is None or delta < best[0]:
            best = (delta, candidate)
    return best[1] if best else None


def _select_best_video(candidates: Iterable[Dict[str, object]]) -> Dict[str, object] | None:
    best: Dict[str, object] | None = None
    preferred_min, preferred_max = LIVE_DURATION_PREFERRED
    for candidate in candidates:
        dur = candidate.get("dur")
        still_time = candidate.get("still_image_time")
        if best is None:
            best = candidate
            continue
        best_dur = best.get("dur")
        if dur is not None and best_dur is not None:
            current_score = _duration_score(dur, preferred_min, preferred_max)
            best_score = _duration_score(best_dur, preferred_min, preferred_max)
            if current_score > best_score:
                best = candidate
                continue
        if still_time is not None and best.get("still_image_time") is not None:
            if still_time < best["still_image_time"]:
                best = candidate
    return best


def _duration_score(duration: float, preferred_min: float, preferred_max: float) -> float:
    if duration < preferred_min:
        return -preferred_min + duration
    if duration > preferred_max:
        return -duration
    midpoint = (preferred_min + preferred_max) / 2
    return preferred_max - abs(midpoint - duration)


def _build_group(photo: Dict[str, object], video: Dict[str, object], confidence: float) -> LiveGroup:
    return LiveGroup(
        id=f"live_{hash((photo['rel'], video['rel'])) & 0xFFFFFF:x}",
        still=photo["rel"],
        motion=video["rel"],
        content_id=video.get("content_id") or photo.get("content_id"),
        still_image_time=video.get("still_image_time"),
        confidence=confidence,
    )
