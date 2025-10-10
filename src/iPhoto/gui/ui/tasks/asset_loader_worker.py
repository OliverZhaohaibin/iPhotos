"""Background worker that assembles asset payloads for the grid views."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from PySide6.QtCore import QObject, QRunnable, Signal

from ....cache.index_store import IndexStore
from ....config import WORK_DIR_NAME
from ....core.pairing import pair_live
from ....media_classifier import classify_media
from ....utils.pathutils import ensure_work_dir


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
        live_map: Dict[str, Dict[str, object]],
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._root = root
        self._featured: Set[str] = {str(entry) for entry in featured}
        self.signals: AssetLoaderSignals = AssetLoaderSignals()
        self._live_map = live_map
        self._is_cancelled = False

    @property
    def root(self) -> Path:
        """Return the album root handled by this worker."""

        return self._root

    def run(self) -> None:  # pragma: no cover - executed on worker thread
        try:
            self._is_cancelled = False
            for chunk in self._build_payload_chunks():
                if self._is_cancelled:
                    break
                if chunk:
                    self.signals.chunkReady.emit(self._root, chunk)
            if not self._is_cancelled:
                self.signals.finished.emit(self._root, True)
            else:
                self.signals.finished.emit(self._root, False)
        except Exception as exc:  # pragma: no cover - surfaced via signal
            if not self._is_cancelled:
                self.signals.error.emit(self._root, str(exc))
            self.signals.finished.emit(self._root, False)

    def cancel(self) -> None:
        """Request cancellation of the current load operation."""

        self._is_cancelled = True

    # ------------------------------------------------------------------
    def _build_payload_chunks(self) -> Iterable[List[Dict[str, object]]]:
        ensure_work_dir(self._root, WORK_DIR_NAME)
        index_rows = list(IndexStore(self._root).read_all())
        live_map = self._resolve_live_map(index_rows, self._live_map)
        motion_paths_to_hide = self._motion_paths_to_hide(live_map)

        total = len(index_rows)
        if total == 0:
            self.signals.progressUpdated.emit(self._root, 0, 0)
            return

        chunk_size = 200
        chunk: List[Dict[str, object]] = []
        last_reported = 0
        for position, row in enumerate(index_rows, start=1):
            if self._is_cancelled:
                return
            should_emit = position == total or position - last_reported >= 50
            rel = str(row.get("rel"))
            if not rel or rel in motion_paths_to_hide:
                if should_emit:
                    last_reported = position
                    self.signals.progressUpdated.emit(self._root, position, total)
                continue

            live_info = live_map.get(rel)
            abs_path = str((self._root / rel).resolve())
            is_image, is_video = classify_media(row)

            live_motion: Optional[str] = None
            live_motion_abs: Optional[str] = None
            live_group_id: Optional[str] = None

            if live_info and live_info.get("role") == "still":
                motion_rel = live_info.get("motion")
                if isinstance(motion_rel, str) and motion_rel:
                    live_motion = motion_rel
                    live_motion_abs = str((self._root / motion_rel).resolve())
                group_id = live_info.get("id")
                if isinstance(group_id, str):
                    live_group_id = group_id
            elif live_info and isinstance(live_info.get("id"), str):
                live_group_id = str(live_info["id"])

            entry: Dict[str, object] = {
                "rel": rel,
                "abs": abs_path,
                "id": row.get("id", rel),
                "name": Path(rel).name,
                "is_current": False,
                "is_image": is_image,
                "is_video": is_video,
                "is_live": bool(live_motion),
                "live_group_id": live_group_id,
                "live_motion": live_motion,
                "live_motion_abs": live_motion_abs,
                "size": self._determine_size(row, is_image),
                "dt": row.get("dt"),
                "featured": self._is_featured(rel),
                "still_image_time": row.get("still_image_time"),
                "dur": row.get("dur"),
            }
            chunk.append(entry)
            if should_emit:
                last_reported = position
                self.signals.progressUpdated.emit(self._root, position, total)

            if len(chunk) >= chunk_size or position == total:
                yield chunk
                chunk = []

        if chunk:
            yield chunk

    def _motion_paths_to_hide(self, live_map: Dict[str, Dict[str, object]]) -> Set[str]:
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

    def _determine_size(self, row: Dict[str, object], is_image: bool) -> object:
        if is_image:
            return (row.get("w"), row.get("h"))
        return {"bytes": row.get("bytes"), "duration": row.get("dur")}

    def _is_featured(self, rel: str) -> bool:
        if rel in self._featured:
            return True
        live_ref = f"{rel}#live"
        return live_ref in self._featured

    def _resolve_live_map(
        self,
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
