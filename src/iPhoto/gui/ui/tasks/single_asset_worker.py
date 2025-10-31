"""Worker that refreshes metadata for a single asset without a full rescan."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import List

from PySide6.QtCore import QObject, QRunnable, Signal

from ....cache.index_store import IndexStore
from ....config import WORK_DIR_NAME
from ....io.scanner import process_media_paths
from ....utils.pathutils import ensure_work_dir


class SingleAssetSignals(QObject):
    """Signals emitted by :class:`SingleAssetWorker` during execution."""

    finished = Signal(Path, dict)
    """Emitted with the album root and refreshed index row."""

    error = Signal(Path, str)
    """Emitted when metadata refresh fails, providing the root and reason."""


class SingleAssetWorker(QRunnable):
    """Recompute metadata for a single asset and update ``index.jsonl``."""

    def __init__(self, album_root: Path, asset_path: Path, signals: SingleAssetSignals) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._album_root = album_root
        self._asset_path = asset_path
        # ``BackgroundTaskManager`` expects workers to expose a ``signals``
        # attribute so it can keep the container alive until the job completes.
        self.signals = signals
        self._signals = signals

    def run(self) -> None:  # type: ignore[override]
        """Execute the metadata refresh and persist the updated row."""

        try:
            ensure_work_dir(self._album_root, WORK_DIR_NAME)

            if not self._asset_path.exists():
                raise FileNotFoundError(self._asset_path)

            # ``process_media_paths`` expects image and video buckets.  Routing
            # the asset through the appropriate list preserves specialised video
            # handling such as still-image extraction and duration metadata.
            image_paths: List[Path] = []
            video_paths: List[Path] = []
            mime_type, _ = mimetypes.guess_type(self._asset_path.name)
            if mime_type and mime_type.startswith("video/"):
                video_paths.append(self._asset_path)
            else:
                image_paths.append(self._asset_path)

            rows = list(process_media_paths(self._album_root, image_paths, video_paths))
            if not rows:
                raise RuntimeError(f"Failed to refresh metadata for {self._asset_path}")

            row = rows[0]
            rel_value = row.get("rel")
            if not isinstance(rel_value, str) or not rel_value:
                raise RuntimeError("Refreshed index row did not include a 'rel' key")

            IndexStore(self._album_root).upsert_row(rel_value, row)
        except Exception as exc:  # pragma: no cover - defensive path
            self._signals.error.emit(self._album_root, str(exc))
            return

        self._signals.finished.emit(self._album_root, row)


__all__ = ["SingleAssetSignals", "SingleAssetWorker"]
