"""Background worker that assembles asset payloads for the grid views."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from PySide6.QtCore import QObject, QRunnable, Signal

from ....cache.index_store import IndexStore
from ....config import WORK_DIR_NAME
from ....core.pairing import pair_live
from ....media_classifier import classify_media
from ....utils.geocoding import resolve_location_name
from ....utils.pathutils import ensure_work_dir


def _normalize_featured(featured: Iterable[str]) -> Set[str]:
    return {str(entry) for entry in featured}


def _determine_size(row: Dict[str, object], is_image: bool) -> object:
    if is_image:
        return (row.get("w"), row.get("h"))
    return {"bytes": row.get("bytes"), "duration": row.get("dur")}


def _is_panorama_candidate(row: Dict[str, object], is_image: bool) -> bool:
    """Return ``True`` when *row* looks like a panorama photograph.

    The heuristic is intentionally conservative: it only flags assets that are
    confirmed still images, have a wide aspect ratio (width at least twice the
    height), and exceed a minimum size threshold. The size gate helps filter out
    tiny thumbnails or preview files that might also be wide but should not
    display the panorama badge.
    """

    if not is_image:
        return False

    width = row.get("w")
    height = row.get("h")
    byte_size = row.get("bytes")

    if not isinstance(width, int) or not isinstance(height, int):
        return False
    if width <= 0 or height <= 0:
        return False
    if not isinstance(byte_size, int) or byte_size <= 1 * 1024 * 1024:
        return False

    aspect_ratio = width / height
    return aspect_ratio >= 2.0


def _is_featured(rel: str, featured: Set[str]) -> bool:
    if rel in featured:
        return True
    live_ref = f"{rel}#live"
    return live_ref in featured


def _resolve_live_map(
    index_rows: List[Dict[str, object]],
    base_map: Dict[str, Dict[str, object]],
) -> Dict[str, Dict[str, object]]:
    mapping: Dict[str, Dict[str, object]] = dict(base_map)
    missing: Set[str] = set()
    for row in index_rows:
        rel = str(row.get("rel"))
        if not rel:
            continue
        is_image, _ = classify_media(row)
        if not is_image:
            continue
        info = mapping.get(rel)
        motion_ref = info.get("motion") if isinstance(info, dict) else None
        if isinstance(motion_ref, str) and motion_ref:
            continue
        missing.add(rel)
    if not missing:
        return mapping

    for group in pair_live(index_rows):
        still = group.still
        if still not in missing:
            continue
        motion = group.motion
        record: Dict[str, object] = {
            "id": group.id,
            "still": still,
            "motion": motion,
            "confidence": group.confidence,
        }
        if group.content_id:
            record["content_id"] = group.content_id
        if group.still_image_time is not None:
            record["still_image_time"] = group.still_image_time
        mapping[still] = {**record, "role": "still"}
        if motion:
            mapping[motion] = {**record, "role": "motion"}
    return mapping


def _motion_paths_to_hide(live_map: Dict[str, Dict[str, object]]) -> Set[str]:
    motion_paths: Set[str] = set()
    for info in live_map.values():
        if not isinstance(info, dict):
            continue
        if info.get("role") != "motion":
            continue
        motion_rel = info.get("motion")
        if isinstance(motion_rel, str) and motion_rel:
            motion_paths.add(motion_rel)
    return motion_paths


def _build_entry(
    root: Path,
    row: Dict[str, object],
    featured: Set[str],
    live_map: Dict[str, Dict[str, object]],
    motion_paths_to_hide: Set[str],
) -> Optional[Dict[str, object]]:
    rel = str(row.get("rel"))
    if not rel or rel in motion_paths_to_hide:
        return None

    live_info = live_map.get(rel)
    abs_path = str((root / rel).resolve())
    is_image, is_video = classify_media(row)
    is_pano = _is_panorama_candidate(row, is_image)

    live_motion: Optional[str] = None
    live_motion_abs: Optional[str] = None
    live_group_id: Optional[str] = None

    if isinstance(live_info, dict) and live_info.get("role") == "still":
        motion_rel = live_info.get("motion")
        if isinstance(motion_rel, str) and motion_rel:
            live_motion = motion_rel
            live_motion_abs = str((root / motion_rel).resolve())
        group_id = live_info.get("id")
        if isinstance(group_id, str):
            live_group_id = group_id
    elif isinstance(live_info, dict) and isinstance(live_info.get("id"), str):
        live_group_id = str(live_info["id"])

    gps_raw = row.get("gps") if isinstance(row, dict) else None
    location_name = resolve_location_name(gps_raw if isinstance(gps_raw, dict) else None)

    entry: Dict[str, object] = {
        "rel": rel,
        "abs": abs_path,
        "id": row.get("id", rel),
        "name": Path(rel).name,
        "is_current": False,
        "is_image": is_image,
        "is_video": is_video,
        "is_live": bool(live_motion),
        "is_pano": is_pano,
        "live_group_id": live_group_id,
        "live_motion": live_motion,
        "live_motion_abs": live_motion_abs,
        "size": _determine_size(row, is_image),
        "dt": row.get("dt"),
        "featured": _is_featured(rel, featured),
        "still_image_time": row.get("still_image_time"),
        "dur": row.get("dur"),
        "location": location_name,
        "gps": gps_raw,
        "bytes": row.get("bytes"),
        "mime": row.get("mime"),
        "make": row.get("make"),
        "model": row.get("model"),
        "lens": row.get("lens"),
        "iso": row.get("iso"),
        "f_number": row.get("f_number"),
        "exposure_time": row.get("exposure_time"),
        "exposure_compensation": row.get("exposure_compensation"),
        "focal_length": row.get("focal_length"),
        "w": row.get("w"),
        "h": row.get("h"),
        "content_id": row.get("content_id"),
        "frame_rate": row.get("frame_rate"),
        "codec": row.get("codec"),
        # Include the original location for trashed assets so restore flows can
        # display accurate targeting information and round-trip the metadata.
        "original_rel_path": row.get("original_rel_path"),
        # Record the originating album identifier and the asset's relative path
        # inside that album so restores remain resilient to folder renames.
        "original_album_id": row.get("original_album_id"),
        "original_album_subpath": row.get("original_album_subpath"),
    }
    return entry


def compute_asset_rows(
    root: Path,
    featured: Iterable[str],
    live_map: Dict[str, Dict[str, object]],
) -> Tuple[List[Dict[str, object]], int]:
    ensure_work_dir(root, WORK_DIR_NAME)
    index_rows = list(IndexStore(root).read_all())
    resolved_map = _resolve_live_map(index_rows, live_map)
    motion_paths = _motion_paths_to_hide(resolved_map)
    featured_set = _normalize_featured(featured)

    entries: List[Dict[str, object]] = []
    for row in index_rows:
        entry = _build_entry(root, row, featured_set, resolved_map, motion_paths)
        if entry is not None:
            entries.append(entry)
    return entries, len(index_rows)


class AssetLoaderSignals(QObject):
    """Signal container for :class:`AssetLoaderWorker` events."""

    progressUpdated = Signal(Path, int, int)
    chunkReady = Signal(Path, list)
    finished = Signal(Path, bool)
    error = Signal(Path, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)


class AssetLoaderWorker(QRunnable):
    """Load album assets on a background thread."""

    def __init__(
        self,
        root: Path,
        featured: Iterable[str],
        signals: AssetLoaderSignals,
        live_map: Dict[str, Dict[str, object]],
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._root = root
        self._featured: Set[str] = _normalize_featured(featured)
        self._signals = signals
        self._live_map = live_map
        self._is_cancelled = False

    @property
    def root(self) -> Path:
        """Return the album root handled by this worker."""

        return self._root

    @property
    def signals(self) -> AssetLoaderSignals:
        """Expose the worker signals for connection management."""

        return self._signals

    def run(self) -> None:  # pragma: no cover - executed on worker thread
        try:
            self._is_cancelled = False
            for chunk in self._build_payload_chunks():
                if self._is_cancelled:
                    break
                if chunk:
                    self._signals.chunkReady.emit(self._root, chunk)
            if not self._is_cancelled:
                self._signals.finished.emit(self._root, True)
            else:
                self._signals.finished.emit(self._root, False)
        except Exception as exc:  # pragma: no cover - surfaced via signal
            if not self._is_cancelled:
                self._signals.error.emit(self._root, str(exc))
            self._signals.finished.emit(self._root, False)

    def cancel(self) -> None:
        """Request cancellation of the current load operation."""

        self._is_cancelled = True

    # ------------------------------------------------------------------
    def _build_payload_chunks(self) -> Iterable[List[Dict[str, object]]]:
        ensure_work_dir(self._root, WORK_DIR_NAME)
        index_rows = list(IndexStore(self._root).read_all())
        live_map = _resolve_live_map(index_rows, self._live_map)
        motion_paths_to_hide = _motion_paths_to_hide(live_map)

        total = len(index_rows)
        if total == 0:
            self._signals.progressUpdated.emit(self._root, 0, 0)
            return

        chunk_size = 200
        chunk: List[Dict[str, object]] = []
        last_reported = 0
        for position, row in enumerate(index_rows, start=1):
            if self._is_cancelled:
                return
            should_emit = position == total or position - last_reported >= 50
            entry = _build_entry(
                self._root,
                row,
                self._featured,
                live_map,
                motion_paths_to_hide,
            )
            if entry is not None:
                chunk.append(entry)
            if should_emit:
                last_reported = position
                self._signals.progressUpdated.emit(self._root, position, total)

            if chunk and (len(chunk) >= chunk_size or position == total):
                yield chunk
                chunk = []

        if chunk:
            yield chunk
