"""Background worker that assembles asset payloads for the grid views."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set

from PySide6.QtCore import QObject, QRunnable, Signal

from ....cache.index_store import IndexStore
from ....config import WORK_DIR_NAME
from ....errors import IndexCorruptedError
from ....media_classifier import classify_media
from ....utils.pathutils import ensure_work_dir
from ..models.live_map import load_live_map


class AssetLoaderWorker(QObject, QRunnable):
    """Load album assets on a background thread."""

    progressUpdated = Signal(object, int, int)
    chunkReady = Signal(object, list)
    finished = Signal(object, bool)
    error = Signal(object, str)

    CACHE_MISSING_PREFIX = "CACHE_MISSING:"

    def __init__(self, root: Path, featured: Iterable[str]) -> None:
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.setAutoDelete(False)
        self._root = root
        self._featured: Set[str] = {str(entry) for entry in featured}

    def run(self) -> None:  # pragma: no cover - executed on worker thread
        try:
            for chunk in self._build_payload_chunks():
                if chunk:
                    self.chunkReady.emit(self._root, chunk)
            self.finished.emit(self._root, True)
        except FileNotFoundError as exc:  # pragma: no cover - surfaced via signal
            message = (
                f"{self.CACHE_MISSING_PREFIX}Cache files disappeared during load: {exc}"
            )
            self.error.emit(self._root, message)
            self.finished.emit(self._root, False)
        except IndexCorruptedError as exc:  # pragma: no cover - surfaced via signal
            self.error.emit(self._root, str(exc))
            self.finished.emit(self._root, False)
        except Exception as exc:  # pragma: no cover - surfaced via signal
            self.error.emit(self._root, str(exc))
            self.finished.emit(self._root, False)

    # ------------------------------------------------------------------
    def _build_payload_chunks(self) -> Iterable[List[Dict[str, object]]]:
        ensure_work_dir(self._root, WORK_DIR_NAME)
        store = IndexStore(self._root)
        if not store.path.exists():
            raise FileNotFoundError(store.path)

        live_map = self._resolve_live_map(load_live_map(self._root))
        motion_paths_to_hide = self._motion_paths_to_hide(live_map)

        chunk_size = 200
        chunk: List[Dict[str, object]] = []
        processed = 0
        last_reported = 0

        try:
            rows_iter = store.read_all()
        except IndexCorruptedError:
            raise

        def _rows() -> Iterator[Dict[str, object]]:
            try:
                yield from rows_iter
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise FileNotFoundError(store.path) from exc

        for row in _rows():
            processed += 1
            should_emit = processed - last_reported >= 50
            rel = str(row.get("rel"))
            if not rel or rel in motion_paths_to_hide:
                if should_emit:
                    last_reported = processed
                    self.progressUpdated.emit(self._root, processed, -1)
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
                last_reported = processed
                self.progressUpdated.emit(self._root, processed, -1)

            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []

        if chunk:
            yield chunk
        if processed == 0:
            self.progressUpdated.emit(self._root, 0, 0)
        else:
            self.progressUpdated.emit(self._root, processed, processed)

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
        self, base_map: Dict[str, Dict[str, object]]
    ) -> Dict[str, Dict[str, object]]:
        return dict(base_map)
