"""List model combining ``index.jsonl`` and ``links.json`` data."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

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
from ..tasks.asset_loader_worker import (
    AssetLoaderSignals,
    AssetLoaderWorker,
    compute_asset_rows,
)
from ..tasks.thumbnail_loader import ThumbnailLoader
from .live_map import load_live_map
from .roles import Roles, role_names

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from ...facade import AppFacade


class AssetListModel(QAbstractListModel):
    """Expose album assets to Qt views."""

    loadProgress = Signal(object, int, int)
    loadFinished = Signal(object, bool)

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
        # ``_pending_move_rows`` tracks optimistic UI updates performed when
        # assets are moved out of the virtual "All Photos" collection.  The
        # mapping allows the model to reconcile the optimistic path guesses
        # with the final filesystem locations once the worker thread completes
        # the move, or to roll back the changes if the operation fails.
        self._pending_move_rows: Dict[str, Tuple[int, str]] = {}

    def album_root(self) -> Optional[Path]:
        """Return the path of the currently open album, if any."""

        return self._album_root

    def remove_rows(self, indexes: list[QModelIndex]) -> None:
        """Remove assets referenced by *indexes*, tolerating proxy selections.

        The gallery view exposes :class:`AssetModel`, so any selection reported
        by the view may reference one or more proxy layers.  The helper performs
        the necessary ``mapToSource`` hops before trimming ``self._rows`` so
        callers do not have to special-case filtered or sorted presentations.
        """

        # Mapping proxy indices back to the source model is necessary because the
        # gallery view operates on :class:`AssetModel`, which layers filtering on
        # top of :class:`AssetListModel`.  Walk through any proxy chain by
        # repeatedly invoking ``mapToSource`` until we arrive at indices owned by
        # this model.  Invalid or foreign indices are ignored gracefully so the
        # caller can forward the selection from a view without bespoke checks.
        source_rows: set[int] = set()
        for proxy_index in indexes:
            if not proxy_index.isValid() or proxy_index.column() != 0:
                continue
            current_index = proxy_index
            model = current_index.model()
            while model is not self and hasattr(model, "mapToSource"):
                mapped_index = model.mapToSource(current_index)
                if not mapped_index.isValid():
                    current_index = QModelIndex()
                    break
                current_index = mapped_index
                model = current_index.model()
            if not current_index.isValid() or model is not self:
                continue
            row_number = current_index.row()
            if 0 <= row_number < len(self._rows):
                source_rows.add(row_number)

        if not source_rows:
            return

        # Removing rows in descending order prevents index shifts from
        # invalidating pending deletions.  The thumbnail cache and lookup tables
        # are updated to remain in sync with the trimmed dataset.
        for row in sorted(source_rows, reverse=True):
            row_data = self._rows[row]
            rel_key = str(row_data["rel"])
            self.beginRemoveRows(QModelIndex(), row, row)
            self._rows.pop(row)
            self.endRemoveRows()
            self._row_lookup.pop(rel_key, None)
            self._thumb_cache.pop(rel_key, None)

        # Rebuild lookup tables so callers relying on ``_row_lookup`` continue to
        # receive accurate mappings.  Clearing caches keeps the thumbnail loader
        # aligned with the new model contents at the cost of recomputing a few
        # placeholders lazily when they are next requested.
        self._row_lookup = {row["rel"]: index for index, row in enumerate(self._rows)}
        self._placeholder_cache.clear()
        self._visible_rows.clear()

    def update_rows_for_move(
        self, rels: list[str], destination_root: Path
    ) -> None:
        """Optimistically update moved rows to reflect their new location.

        The gallery keeps the "All Photos" aggregate visually stable by
        updating only the affected rows when files are moved into a concrete
        album.  The method records enough bookkeeping to reconcile the final
        filesystem locations once the worker thread finishes the move.
        """

        if not self._album_root or not rels:
            return

        album_root = self._album_root.resolve()
        try:
            destination_root = destination_root.resolve()
            dest_prefix = destination_root.relative_to(album_root)
        except OSError:
            return
        except ValueError:
            # Ignore moves that target folders outside the currently open
            # library root.  They are not visible in this model so an
            # optimistic update would be misleading.
            return

        changed_rows: List[int] = []
        for original_rel in {Path(rel).as_posix() for rel in rels}:
            row_index = self._row_lookup.get(original_rel)
            if row_index is None:
                continue

            row_data = self._rows[row_index]
            file_name = Path(original_rel).name
            if str(dest_prefix) in (".", ""):
                guessed_rel = file_name
            else:
                guessed_rel = (dest_prefix / file_name).as_posix()
            guessed_abs = destination_root / file_name

            # Re-key caches and lookup tables before mutating the row so all
            # helper structures stay consistent with the optimistic update.
            self._row_lookup.pop(original_rel, None)
            self._row_lookup[guessed_rel] = row_index
            thumb = self._thumb_cache.pop(original_rel, None)
            if thumb is not None:
                self._thumb_cache[guessed_rel] = thumb
            placeholder = self._placeholder_cache.pop(original_rel, None)
            if placeholder is not None:
                self._placeholder_cache[guessed_rel] = placeholder

            row_data["rel"] = guessed_rel
            row_data["abs"] = str(guessed_abs)
            self._pending_move_rows[original_rel] = (row_index, guessed_rel)
            changed_rows.append(row_index)

        for row in changed_rows:
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index,
                model_index,
                [Roles.REL, Roles.ABS, Qt.DecorationRole],
            )

    def finalise_move_results(self, moves: List[Tuple[Path, Path]]) -> None:
        """Reconcile optimistic move updates with the worker results."""

        if not self._album_root or not moves:
            return

        album_root = self._album_root.resolve()
        updated_rows: List[int] = []
        for original_path, target_path in moves:
            try:
                original_rel = original_path.resolve().relative_to(album_root).as_posix()
            except OSError:
                continue
            except ValueError:
                continue

            pending = self._pending_move_rows.pop(original_rel, None)
            if pending is None:
                continue
            row_index, guessed_rel = pending
            row_data = self._rows[row_index]

            try:
                final_rel = target_path.resolve().relative_to(album_root).as_posix()
            except OSError:
                final_rel = guessed_rel
            except ValueError:
                final_rel = guessed_rel

            self._row_lookup.pop(guessed_rel, None)
            self._row_lookup[final_rel] = row_index
            thumb = self._thumb_cache.pop(guessed_rel, None)
            if thumb is not None:
                self._thumb_cache[final_rel] = thumb
            placeholder = self._placeholder_cache.pop(guessed_rel, None)
            if placeholder is not None:
                self._placeholder_cache[final_rel] = placeholder

            row_data["rel"] = final_rel
            row_data["abs"] = str(target_path.resolve())
            updated_rows.append(row_index)

        for row in updated_rows:
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index,
                model_index,
                [Roles.REL, Roles.ABS, Qt.DecorationRole],
            )

    def rollback_pending_moves(self) -> None:
        """Restore original metadata for moves that failed or were cancelled."""

        if not self._album_root or not self._pending_move_rows:
            return

        album_root = self._album_root.resolve()
        to_restore = list(self._pending_move_rows.items())
        self._pending_move_rows.clear()

        restored_rows: List[int] = []
        for original_rel, (row_index, guessed_rel) in to_restore:
            row_data = self._rows[row_index]
            absolute = (album_root / original_rel).resolve()

            self._row_lookup.pop(guessed_rel, None)
            self._row_lookup[original_rel] = row_index
            thumb = self._thumb_cache.pop(guessed_rel, None)
            if thumb is not None:
                self._thumb_cache[original_rel] = thumb
            placeholder = self._placeholder_cache.pop(guessed_rel, None)
            if placeholder is not None:
                self._placeholder_cache[original_rel] = placeholder

            row_data["rel"] = original_rel
            row_data["abs"] = str(absolute)
            restored_rows.append(row_index)

        for row in restored_rows:
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index,
                model_index,
                [Roles.REL, Roles.ABS, Qt.DecorationRole],
            )

    def has_pending_move_placeholders(self) -> bool:
        """Return ``True`` when optimistic move updates are awaiting results."""

        return bool(self._pending_move_rows)

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
        self._rows = rows
        self._row_lookup = {row["rel"]: index for index, row in enumerate(rows)}
        active = set(self._row_lookup.keys())
        self._thumb_cache = {
            rel: pix for rel, pix in self._thumb_cache.items() if rel in active
        }
        self.endResetModel()

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
        if role == Roles.IS_PANO:
            return row.get("is_pano", False)
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
        if role == Roles.LOCATION:
            return row.get("location")
        if role == Roles.FEATURED:
            return row["featured"]
        if role == Roles.IS_CURRENT:
            return bool(row.get("is_current", False))
        if role == Roles.INFO:
            return dict(row)
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
        self._thumb_loader.reset_for_album(root)
        self.beginResetModel()
        self._rows = []
        self._row_lookup = {}
        self._thumb_cache.clear()
        self.endResetModel()

    def update_featured_status(self, rel: str, is_featured: bool) -> None:
        """Update the cached ``featured`` flag for the asset identified by *rel*."""

        rel_key = str(rel)
        row_index = self._row_lookup.get(rel_key)
        if row_index is None or not (0 <= row_index < len(self._rows)):
            return

        row = self._rows[row_index]
        current = bool(row.get("featured", False))
        normalized = bool(is_featured)
        if current == normalized:
            return

        row["featured"] = normalized
        model_index = self.index(row_index, 0)
        self.dataChanged.emit(model_index, model_index, [Roles.FEATURED])

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

        worker = AssetLoaderWorker(self._album_root, featured, signals, live_map)
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
