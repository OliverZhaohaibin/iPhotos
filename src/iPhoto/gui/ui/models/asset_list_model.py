"""List model combining ``index.jsonl`` and ``links.json`` data."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSize,
    Qt,
    QThreadPool,
    Signal,
    QTimer,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap

from ....config import WORK_DIR_NAME
from ....utils.geocoding import ReverseGeocoder
from ..tasks.asset_loader_worker import (
    AssetLoaderSignals,
    AssetLoaderWorker,
    compute_asset_rows,
)
from ..tasks.geocoding_worker import GeocodingWorker, geocoding_pool
from ..tasks.thumbnail_loader import ThumbnailLoader
from .live_map import load_live_map
from .roles import Roles, role_names

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from ...facade import AppFacade


class AssetListModel(QAbstractListModel):
    """Expose album assets to Qt views."""

    loadProgress = Signal(object, int, int)
    loadFinished = Signal(object, bool)
    locationDataReady = Signal(str, str)

    def __init__(self, facade: "AppFacade", parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._facade = facade
        self._album_root: Optional[Path] = None
        self._rows: List[Dict[str, object]] = []
        self._row_lookup: Dict[str, int] = {}
        self._thumb_cache: Dict[str, QPixmap] = {}
        self._placeholder_cache: Dict[str, QPixmap] = {}
        self._thumb_size = QSize(192, 192)
        self._thumb_loader = ThumbnailLoader(self)
        self._thumb_loader.ready.connect(self._on_thumb_ready)
        self._loader_pool = QThreadPool.globalInstance()
        self._loader_worker: Optional[AssetLoaderWorker] = None
        self._loader_signals: Optional[AssetLoaderSignals] = None
        self._pending_reload = False
        self._visible_rows: Set[int] = set()
        self.locationDataReady.connect(self._on_location_ready)
        self._geocoder: ReverseGeocoder | None = None
        self._geocode_lock = Lock()
        self._scheduled_geocodes: Set[str] = set()

    def album_root(self) -> Optional[Path]:
        """Return the path of the currently open album, if any."""

        return self._album_root

    def populate_from_cache(self, *, max_index_bytes: int = 512 * 1024) -> bool:
        """Synchronously load cached index data when the file is small."""

        if not self._album_root:
            return False
        if self._loader_worker is not None:
            return False

        root = self._album_root
        index_path = root / WORK_DIR_NAME / "index.jsonl"
        try:
            size = index_path.stat().st_size
        except OSError:
            size = 0
        if size > max_index_bytes:
            return False

        manifest = self._facade.current_album.manifest if self._facade.current_album else {}
        featured = manifest.get("featured", []) or []
        live_map = load_live_map(root)

        try:
            rows, total = compute_asset_rows(root, featured, live_map)
        except Exception as exc:  # pragma: no cover - surfaced via GUI
            self._facade.errorRaised.emit(str(exc))
            self.loadFinished.emit(root, False)
            return False

        self.beginResetModel()
        with self._geocode_lock:
            self._scheduled_geocodes.clear()
        self._rows = rows
        self._row_lookup = {row["rel"]: index for index, row in enumerate(rows)}
        active = set(self._row_lookup.keys())
        self._thumb_cache = {
            rel: pix for rel, pix in self._thumb_cache.items() if rel in active
        }
        self.endResetModel()
        self._schedule_geocode_for_rows(self._rows)

        self._pending_reload = False
        self.loadProgress.emit(root, total, total)
        self.loadFinished.emit(root, True)
        return True

    # ------------------------------------------------------------------
    # Qt model implementation
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex | None = None) -> int:  # type: ignore[override]
        if parent is not None and parent.isValid():  # pragma: no cover - tree fallback
            return 0
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # type: ignore[override]
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == Qt.DisplayRole:
            return ""
        if role == Qt.DecorationRole:
            return self._resolve_thumbnail(row)
        if role == Qt.SizeHintRole:
            return QSize(self._thumb_size.width(), self._thumb_size.height())
        if role == Roles.REL:
            return row["rel"]
        if role == Roles.ABS:
            return row["abs"]
        if role == Roles.ASSET_ID:
            return row["id"]
        if role == Roles.IS_IMAGE:
            return row["is_image"]
        if role == Roles.IS_VIDEO:
            return row["is_video"]
        if role == Roles.IS_LIVE:
            return row["is_live"]
        if role == Roles.LIVE_GROUP_ID:
            return row["live_group_id"]
        if role == Roles.LIVE_MOTION_REL:
            return row["live_motion"]
        if role == Roles.LIVE_MOTION_ABS:
            return row["live_motion_abs"]
        if role == Roles.SIZE:
            return row["size"]
        if role == Roles.DT:
            return row["dt"]
        if role == Roles.FEATURED:
            return row["featured"]
        if role == Roles.LOCATION_INFO:
            return row.get("location")
        if role == Roles.IS_CURRENT:
            return bool(row.get("is_current", False))
        return None

    def roleNames(self) -> Dict[int, bytes]:  # type: ignore[override]
        return role_names(super().roleNames())

    def setData(
        self, index: QModelIndex, value: Any, role: int = Qt.EditRole
    ) -> bool:  # type: ignore[override]
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return False
        if role != Roles.IS_CURRENT:
            return super().setData(index, value, role)

        normalized = bool(value)
        row = self._rows[index.row()]
        if bool(row.get("is_current", False)) == normalized:
            return True
        row["is_current"] = normalized
        self.dataChanged.emit(index, index, [Roles.IS_CURRENT])
        return True

    def thumbnail_loader(self) -> ThumbnailLoader:
        return self._thumb_loader

    # ------------------------------------------------------------------
    # Facade callbacks
    # ------------------------------------------------------------------
    def prepare_for_album(self, root: Path) -> None:
        """Reset internal state so *root* becomes the active album."""

        if self._loader_worker:
            self._loader_worker.cancel()
        self._pending_reload = False
        self._album_root = root
        self._geocoder = ReverseGeocoder.for_album(root)
        with self._geocode_lock:
            self._scheduled_geocodes.clear()
        self._thumb_loader.reset_for_album(root)
        self.beginResetModel()
        self._rows = []
        self._row_lookup = {}
        self._thumb_cache.clear()
        self.endResetModel()

    # ------------------------------------------------------------------
    # Data loading helpers
    # ------------------------------------------------------------------
    def start_load(self) -> None:
        if not self._album_root:
            return
        if self._loader_worker is not None:
            self._loader_worker.cancel()
            self._pending_reload = True
            return
        with self._geocode_lock:
            self._scheduled_geocodes.clear()
        self.beginResetModel()
        self._rows = []
        self._row_lookup = {}
        self.endResetModel()
        manifest = self._facade.current_album.manifest if self._facade.current_album else {}
        featured = manifest.get("featured", []) or []
        signals = AssetLoaderSignals()
        signals.progressUpdated.connect(self._on_loader_progress)
        signals.chunkReady.connect(self._on_loader_chunk_ready)
        signals.finished.connect(self._on_loader_finished)
        signals.error.connect(self._on_loader_error)

        live_map = load_live_map(self._album_root)

        worker = AssetLoaderWorker(
            self._album_root,
            featured,
            signals,
            live_map,
            self._handle_geocode_result,
        )
        self._loader_worker = worker
        self._loader_signals = signals
        self._pending_reload = False
        self._loader_pool.start(worker)

    def _on_loader_chunk_ready(self, root: Path, chunk: List[Dict[str, object]]) -> None:
        if not self._loader_worker or root != self._loader_worker.root:
            return
        if not self._album_root or root != self._album_root or not chunk:
            return
        start_row = len(self._rows)
        end_row = start_row + len(chunk) - 1
        self.beginInsertRows(QModelIndex(), start_row, end_row)
        self._rows.extend(chunk)
        for offset, row_data in enumerate(chunk):
            self._row_lookup[row_data["rel"]] = start_row + offset
        self.endInsertRows()

    def _on_loader_progress(self, root: Path, current: int, total: int) -> None:
        if not self._loader_worker or root != self._loader_worker.root:
            return
        if not self._album_root or root != self._album_root:
            return
        self.loadProgress.emit(root, current, total)

    def _on_loader_finished(self, root: Path, success: bool) -> None:
        if not self._loader_worker or root != self._loader_worker.root:
            return
        if not self._album_root or root != self._album_root:
            should_restart = bool(self._pending_reload and self._album_root)
            self._teardown_loader()
            if should_restart:
                QTimer.singleShot(0, self.start_load)
            return
        if success:
            active = set(self._row_lookup.keys())
            self._thumb_cache = {
                rel: pix for rel, pix in self._thumb_cache.items() if rel in active
            }
        self.loadFinished.emit(root, success)
        should_restart = bool(self._pending_reload and self._album_root and root == self._album_root)
        self._pending_reload = False
        self._teardown_loader()
        if should_restart:
            QTimer.singleShot(0, self.start_load)

    def _on_loader_error(self, root: Path, message: str) -> None:
        if not self._loader_worker or root != self._loader_worker.root:
            return
        if self._album_root and root == self._album_root:
            self._facade.errorRaised.emit(message)
        should_restart = bool(self._pending_reload and self._album_root)
        self.loadFinished.emit(root, False)
        self._pending_reload = False
        self._teardown_loader()
        if should_restart:
            QTimer.singleShot(0, self.start_load)

    def _teardown_loader(self) -> None:
        if self._loader_worker is not None:
            # ``deleteLater`` is safe even if the worker has already
            # completed and the signal object is otherwise parent-less.
            self._loader_worker.signals.deleteLater()
        elif self._loader_signals is not None:
            self._loader_signals.deleteLater()
        self._loader_worker = None
        self._loader_signals = None
        self._pending_reload = False

    def _ensure_geocoder(self) -> ReverseGeocoder | None:
        if self._geocoder is not None:
            return self._geocoder
        if not self._album_root:
            return None
        self._geocoder = ReverseGeocoder.for_album(self._album_root)
        return self._geocoder

    def _schedule_geocode_for_rows(self, rows: List[Dict[str, object]]) -> None:
        geocoder = self._ensure_geocoder()
        if geocoder is None:
            return
        for row in rows:
            self._schedule_geocode(row, geocoder)

    def _schedule_geocode(self, row: Dict[str, object], geocoder: ReverseGeocoder) -> None:
        if isinstance(row.get("location"), str) and row["location"].strip():
            return
        rel = row.get("rel")
        if not isinstance(rel, str) or not rel:
            return
        gps = row.get("gps")
        if not isinstance(gps, dict):
            return
        lat = gps.get("lat")
        lon = gps.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return
        with self._geocode_lock:
            if rel in self._scheduled_geocodes:
                return
            self._scheduled_geocodes.add(rel)
        worker = GeocodingWorker(
            geocoder,
            rel,
            float(lat),
            float(lon),
            self._handle_geocode_result,
        )
        geocoding_pool().start(worker)

    def _handle_geocode_result(self, rel: str, location: Optional[str]) -> None:
        self.locationDataReady.emit(rel, location or "")

    def _on_location_ready(self, rel: str, location: str) -> None:
        with self._geocode_lock:
            self._scheduled_geocodes.discard(rel)
        if not rel:
            return
        index = self._row_lookup.get(rel)
        if index is None or not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        normalized = location.strip()
        if normalized:
            row["location"] = normalized
        else:
            row.pop("location", None)
        model_index = self.index(index, 0)
        self.dataChanged.emit(model_index, model_index, [Roles.LOCATION_INFO])

    # ------------------------------------------------------------------
    # Thumbnail helpers
    # ------------------------------------------------------------------
    def prioritize_rows(self, first: int, last: int) -> None:
        """Request high-priority thumbnails for the inclusive range *first*→*last*."""

        if not self._rows:
            self._visible_rows.clear()
            return

        if first > last:
            first, last = last, first

        first = max(first, 0)
        last = min(last, len(self._rows) - 1)
        if first > last:
            self._visible_rows.clear()
            return

        requested = set(range(first, last + 1))
        if not requested:
            self._visible_rows.clear()
            return

        uncached = {
            row
            for row in requested
            if str(self._rows[row]["rel"]) not in self._thumb_cache
        }
        if not uncached:
            self._visible_rows = requested
            return
        if uncached.issubset(self._visible_rows):
            self._visible_rows = requested
            return

        self._visible_rows = requested
        for row in range(first, last + 1):
            if row not in uncached:
                continue
            row_data = self._rows[row]
            self._resolve_thumbnail(row_data, ThumbnailLoader.Priority.VISIBLE)

    def _resolve_thumbnail(
        self,
        row: Dict[str, object],
        priority: ThumbnailLoader.Priority = ThumbnailLoader.Priority.NORMAL,
    ) -> QPixmap:
        rel = str(row["rel"])
        cached = self._thumb_cache.get(rel)
        if cached is not None:
            return cached
        placeholder = self._placeholder_for(rel, bool(row.get("is_video")))
        if not self._album_root:
            return placeholder
        abs_path = Path(str(row["abs"]))
        if bool(row.get("is_image")):
            pixmap = self._thumb_loader.request(
                rel,
                abs_path,
                self._thumb_size,
                is_image=True,
                priority=priority,
            )
            if pixmap is not None:
                self._thumb_cache[rel] = pixmap
                return pixmap
        if bool(row.get("is_video")):
            still_time = row.get("still_image_time")
            duration = row.get("dur")
            still_hint: Optional[float] = float(still_time) if isinstance(still_time, (int, float)) else None
            duration_value: Optional[float] = float(duration) if isinstance(duration, (int, float)) else None
            if still_hint is not None and duration_value and duration_value > 0:
                max_seek = max(duration_value - 0.01, 0.0)
                if still_hint > max_seek:
                    still_hint = max_seek
            pixmap = self._thumb_loader.request(
                rel,
                abs_path,
                self._thumb_size,
                is_image=False,
                is_video=True,
                still_image_time=still_hint,
                duration=duration_value,
                priority=priority,
            )
            if pixmap is not None:
                self._thumb_cache[rel] = pixmap
                return pixmap
        return placeholder

    def _on_thumb_ready(self, root: Path, rel: str, pixmap: QPixmap) -> None:
        if not self._album_root or root != self._album_root:
            return
        self._thumb_cache[rel] = pixmap
        index = self._row_lookup.get(rel)
        if index is None:
            return
        model_index = self.index(index, 0)
        self.dataChanged.emit(model_index, model_index, [Qt.DecorationRole])

    def _placeholder_for(self, rel: str, is_video: bool) -> QPixmap:
        suffix = Path(rel).suffix.lower().lstrip(".")
        if not suffix:
            suffix = "video" if is_video else "media"
        key = f"{suffix}|{is_video}"
        cached = self._placeholder_cache.get(key)
        if cached is not None:
            return cached
        canvas = QPixmap(self._thumb_size)
        canvas.fill(QColor("#1b1b1b"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor("#f0f0f0"))
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        label = suffix.upper()
        text_width = metrics.horizontalAdvance(label)
        baseline = (canvas.height() + metrics.ascent()) // 2
        painter.drawText((canvas.width() - text_width) // 2, baseline, label)
        painter.end()
        self._placeholder_cache[key] = canvas
        return canvas
