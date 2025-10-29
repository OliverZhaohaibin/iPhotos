"""List model combining ``index.jsonl`` and ``links.json`` data."""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSize,
    Qt,
    QThreadPool,
    Signal,
    Slot,
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


logger = logging.getLogger(__name__)


class AssetListModel(QAbstractListModel):
    """Expose album assets to Qt views."""

    # ``Path`` is used explicitly so that static compilers such as Nuitka can
    # prove that the connected slots accept the same signature.  Relying on the
    # generic ``object`` type confuses Nuitka's patched ``Signal.connect``
    # implementation and results in runtime errors during packaging.
    loadProgress = Signal(Path, int, int)
    loadFinished = Signal(Path, bool)

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
        # ``_pending_virtual_moves`` records optimistic path adjustments
        # performed when assets are moved out of a virtual collection such as
        # "All Photos".  Each entry maps the original relative path to the
        # updated row index and guessed relative path so the model can reconcile
        # the change when the worker thread completes or undo it if the
        # operation fails.  Concrete album views never populate this mapping
        # because they physically remove rows instead of updating paths in
        # place.
        self._pending_virtual_moves: Dict[str, Tuple[int, str, bool]] = {}
        # ``_pending_row_removals`` caches snapshots of rows trimmed
        # optimistically when the move originates from a concrete album view.
        # The cached metadata allows :meth:`rollback_pending_moves` to rebuild
        # the dataset if the asynchronous filesystem operation ultimately
        # fails.
        self._pending_row_removals: List[Tuple[int, Dict[str, object]]] = []
        # ``_recently_removed_rows`` keeps a bounded cache of the most recently
        # removed asset metadata so controller workflows can still resolve
        # information about an item after an optimistic UI update trimmed it
        # from the live dataset.  This is essential for operations such as
        # Live Photo deletion where the still image disappears from the grid
        # before the facade gathers the paired motion clip path.
        self._recently_removed_rows: "OrderedDict[str, Dict[str, object]]" = (
            OrderedDict()
        )
        self._recently_removed_limit = 256
        # ``_live_map`` caches the Live Photo pairings stored in ``links.json``
        # for the currently active album.  Keeping the mapping around lets the
        # model react to ``linksUpdated`` notifications without having to reload
        # the entire dataset from disk.
        self._live_map: Dict[str, Dict[str, object]] = {}
        # ``_suppress_virtual_reload`` prevents the model from resetting the
        # virtual "All Photos" presentation while a move operation is in flight.
        # Aggregated views perform optimistic, in-place updates to keep the grid
        # perfectly still; triggering a reload when the backend broadcasts the
        # matching ``linksUpdated`` signal would undo that effort by resetting
        # the model.  The flag remains active until the user navigates away from
        # the aggregate view, guaranteeing that neither the intermediate rescan
        # progress nor its completion introduces visual flicker.
        self._suppress_virtual_reload: bool = False
        # ``_virtual_move_requires_revisit`` tracks whether a library-wide move
        # asked the model to hold its optimistic state until the user leaves and
        # returns.  While set, ``linksUpdated`` notifications are ignored so the
        # UI stays completely static; the next ``prepare_for_album`` call clears
        # the flag and allows the refreshed dataset to be loaded.
        self._virtual_move_requires_revisit: bool = False

        self._facade.linksUpdated.connect(self.handle_links_updated)

    def album_root(self) -> Optional[Path]:
        """Return the path of the currently open album, if any."""

        return self._album_root

    def metadata_for_absolute_path(self, path: Path) -> Optional[Dict[str, object]]:
        """Return the cached metadata row for *path* if it belongs to the model.

        The asset grid frequently passes absolute filesystem paths around when
        triggering operations such as copy or delete.  Internally the model
        indexes rows by their path relative to :attr:`_album_root`, so this
        helper normalises the provided *path* to the same representation and
        resolves the matching row when possible.  When the file no longer sits
        inside the current root—because it was moved externally or is part of a
        transient virtual collection—the method gracefully falls back to a
        direct absolute comparison so callers still receive metadata whenever it
        is available.
        """

        if not self._rows:
            return None

        album_root = self._album_root
        try:
            normalized_path = path.resolve()
        except OSError:
            normalized_path = path

        if album_root is not None:
            try:
                normalized_root = album_root.resolve()
            except OSError:
                normalized_root = album_root
            try:
                rel_key = normalized_path.relative_to(normalized_root).as_posix()
            except ValueError:
                rel_key = None
            else:
                row_index = self._row_lookup.get(rel_key)
                if row_index is not None and 0 <= row_index < len(self._rows):
                    return self._rows[row_index]

        normalized_str = str(normalized_path)
        for row in self._rows:
            if str(row.get("abs")) == normalized_str:
                return row
        # Fall back to the recently removed cache so operations triggered right
        # after an optimistic removal can still access metadata that is no
        # longer present in ``self._rows``.  The cache mirrors the structure of
        # the live dataset, therefore callers can interact with the returned
        # dictionary exactly as if the row were still part of the model.
        cached = self._recently_removed_rows.get(normalized_str)
        if cached is not None:
            return cached
        return None

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
            abs_key = str(row_data.get("abs"))
            self.beginRemoveRows(QModelIndex(), row, row)
            self._rows.pop(row)
            self.endRemoveRows()
            self._row_lookup.pop(rel_key, None)
            self._thumb_cache.pop(rel_key, None)
            if abs_key:
                # Stash the removed row metadata before it disappears from the
                # live dataset so follow-up commands can still discover details
                # about the asset.  ``OrderedDict`` keeps the cache bounded by
                # discarding the oldest entry once the limit is exceeded.
                self._recently_removed_rows[abs_key] = dict(row_data)
                self._recently_removed_rows.move_to_end(abs_key)
                if len(self._recently_removed_rows) > self._recently_removed_limit:
                    self._recently_removed_rows.popitem(last=False)

        # Rebuild lookup tables so callers relying on ``_row_lookup`` continue to
        # receive accurate mappings.  Clearing caches keeps the thumbnail loader
        # aligned with the new model contents at the cost of recomputing a few
        # placeholders lazily when they are next requested.
        self._row_lookup = {row["rel"]: index for index, row in enumerate(self._rows)}
        self._placeholder_cache.clear()
        self._visible_rows.clear()

    def update_rows_for_move(
        self,
        rels: list[str],
        destination_root: Path,
        *,
        is_source_main_view: bool = False,
    ) -> None:
        """Apply optimistic UI updates when a move operation is queued.

        ``is_source_main_view`` toggles between the two behaviours required by
        the UX guidelines: concrete album views remove rows immediately to avoid
        showing stale placeholders, whereas virtual library-wide collections
        keep the affected entries visible and simply adjust their guessed path
        until the refreshed index arrives.
        """

        if not self._album_root or not rels:
            return

        album_root = self._album_root.resolve()

        if not is_source_main_view:
            rows_to_remove: List[int] = []
            for original_rel in {Path(rel).as_posix() for rel in rels}:
                row_index = self._row_lookup.get(original_rel)
                if row_index is None:
                    continue
                rows_to_remove.append(row_index)

            if not rows_to_remove:
                return

            for row_index in sorted(set(rows_to_remove), reverse=True):
                if not (0 <= row_index < len(self._rows)):
                    continue

                row_snapshot = dict(self._rows[row_index])
                rel_key = str(row_snapshot.get("rel", ""))
                abs_key = str(row_snapshot.get("abs", "")) if row_snapshot.get("abs") else ""

                self._pending_row_removals.append((row_index, row_snapshot))

                self.beginRemoveRows(QModelIndex(), row_index, row_index)
                self._rows.pop(row_index)
                self.endRemoveRows()

                if rel_key:
                    self._row_lookup.pop(rel_key, None)
                    self._thumb_cache.pop(rel_key, None)
                if abs_key:
                    self._recently_removed_rows[abs_key] = dict(row_snapshot)
                    self._recently_removed_rows.move_to_end(abs_key)
                    if len(self._recently_removed_rows) > self._recently_removed_limit:
                        self._recently_removed_rows.popitem(last=False)

            self._row_lookup = {row["rel"]: index for index, row in enumerate(self._rows)}
            self._placeholder_cache.clear()
            self._visible_rows.clear()
            # Concrete album moves already removed the rows that the worker will
            # touch, so resetting the model again when ``linksUpdated`` fires
            # would only introduce a distracting flicker.  Suppress the next
            # reload notification so the grid remains stable while the
            # background rescan persists the new index to disk.
            self._suppress_virtual_reload = True
            return

        try:
            destination_root = destination_root.resolve()
            dest_prefix = destination_root.relative_to(album_root)
        except OSError:
            return
        except ValueError:
            # Destinations outside the current library root are invisible in
            # the active aggregate view, so applying an optimistic update would
            # produce a misleading placeholder.
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
            # ``False`` marks that the row remains visible; rollback should only
            # restore its metadata instead of re-inserting a duplicate entry.
            self._pending_virtual_moves[original_rel] = (row_index, guessed_rel, False)
            changed_rows.append(row_index)

        for row in changed_rows:
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index,
                model_index,
                [Roles.REL, Roles.ABS, Qt.DecorationRole],
            )

        if changed_rows:
            # ``handle_links_updated`` will fire shortly after the worker
            # rewrites ``links.json``.  Prevent the ensuing notification from
            # resetting the model while our optimistic metadata is still being
            # reconciled.
            self._suppress_virtual_reload = True
            # Track that the aggregate view must not refresh until the user
            # revisits it.  The move worker will update the on-disk index in the
            # background; marking the view as "hold" ensures the final
            # ``linksUpdated`` announcement does not reset the grid when the
            # rescan completes.
            self._virtual_move_requires_revisit = True

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

            pending = self._pending_virtual_moves.pop(original_rel, None)
            if pending is None:
                continue
            row_index, guessed_rel, was_removed = pending
            row_data = self._rows[row_index]

            try:
                final_rel = target_path.resolve().relative_to(album_root).as_posix()
            except OSError:
                final_rel = guessed_rel
            except ValueError:
                final_rel = guessed_rel

            if not was_removed:
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

        if self._pending_row_removals:
            # Concrete album moves that removed rows optimistically succeeded,
            # therefore the cached snapshots are no longer required.
            self._pending_row_removals.clear()

    def rollback_pending_moves(self) -> None:
        """Restore original metadata for moves that failed or were cancelled."""

        if not self._album_root:
            return

        album_root = self._album_root.resolve()
        to_restore = list(self._pending_virtual_moves.items())
        self._pending_virtual_moves.clear()

        restored_rows: List[int] = []
        for original_rel, (row_index, guessed_rel, was_removed) in to_restore:
            row_data = self._rows[row_index]
            absolute = (album_root / original_rel).resolve()

            if not was_removed:
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

        if self._pending_row_removals:
            for row_index, row_data in sorted(self._pending_row_removals, key=lambda entry: entry[0]):
                insert_at = min(max(row_index, 0), len(self._rows))
                self.beginInsertRows(QModelIndex(), insert_at, insert_at)
                restored = dict(row_data)
                self._rows.insert(insert_at, restored)
                self.endInsertRows()
                abs_key = str(restored.get("abs", "")) if restored.get("abs") else ""
                if abs_key:
                    self._recently_removed_rows.pop(abs_key, None)
            self._pending_row_removals.clear()
            self._row_lookup = {row["rel"]: index for index, row in enumerate(self._rows)}
            self._thumb_cache.clear()
            self._placeholder_cache.clear()
            self._visible_rows.clear()

        # The failure/cancellation restored the original dataset.  Allow future
        # backend refresh events to reload the view normally.
        self._suppress_virtual_reload = False
        self._virtual_move_requires_revisit = False

    def has_pending_move_placeholders(self) -> bool:
        """Return ``True`` when optimistic move updates are awaiting results."""

        return bool(self._pending_virtual_moves or self._pending_row_removals)

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
        # Remember the persisted Live Photo metadata so change notifications can
        # reuse it without incurring additional disk reads.
        self._live_map = dict(live_map)

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
        self._recently_removed_rows.clear()
        self._pending_virtual_moves.clear()
        self._pending_row_removals.clear()
        self.endResetModel()
        self._live_map = {}
        self._suppress_virtual_reload = False
        self._virtual_move_requires_revisit = False

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
        self._recently_removed_rows.clear()
        self.endResetModel()
        manifest = self._facade.current_album.manifest if self._facade.current_album else {}
        featured = manifest.get("featured", []) or []
        signals = AssetLoaderSignals()
        signals.progressUpdated.connect(self._on_loader_progress)
        signals.chunkReady.connect(self._on_loader_chunk_ready)
        signals.finished.connect(self._on_loader_finished)
        signals.error.connect(self._on_loader_error)

        live_map = load_live_map(self._album_root)
        # Store the snapshot so Live Photo metadata can be refreshed when the
        # backend updates ``links.json`` without requiring a new loader run.
        self._live_map = dict(live_map)

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
            abs_key = str(row_data.get("abs"))
            if abs_key:
                # Remove any stale cache entry so the freshly loaded metadata
                # takes precedence over historical snapshots retained for
                # optimistic UI updates.
                self._recently_removed_rows.pop(abs_key, None)
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

    @Slot(Path)
    def handle_links_updated(self, root: Path) -> None:
        """React to :mod:`links.json` refreshes triggered by the backend.

        The facade emits :pyattr:`linksUpdated` whenever background workers rewrite
        ``links.json`` for the active album.  Live Photo badges depend on the
        relationships stored in that file, so the view needs to update as soon as
        the metadata changes.  Updating happens in two stages:

        * Refresh the in-memory Live Photo cache immediately so rows that are
          already visible flip their badge without waiting for a worker round-trip.
        * If a loader is currently ingesting data, cancel it and request a fresh
          pass once the cancellation has propagated.  Otherwise schedule a new load
          straight away.  Using :func:`QTimer.singleShot` keeps the reload
          asynchronous, which prevents recursive model resets inside the signal
          delivery stack.
        """

        if not self._album_root:
            logger.debug(
                "AssetListModel: linksUpdated ignored because no album root is active."
            )
            return

        album_root = self._normalise_for_compare(self._album_root)
        updated_root = self._normalise_for_compare(Path(root))

        if not self._links_update_targets_current_view(album_root, updated_root):
            logger.debug(
                "AssetListModel: linksUpdated for %s does not affect current root %s.",
                updated_root,
                album_root,
            )
            return

        if self._suppress_virtual_reload:
            if self._virtual_move_requires_revisit:
                logger.debug(
                    "AssetListModel: holding reload for %s until the aggregate view is reopened.",
                    updated_root,
                )
                return

            logger.debug(
                "AssetListModel: finishing temporary suppression for %s after non-aggregate move.",
                updated_root,
            )
            self._suppress_virtual_reload = False
            # Optimistic updates already adjusted the visible rows.  The cached
            # Live Photo metadata can still change asynchronously, so refresh it
            # in place without triggering a full model reset.
            if self._rows:
                self._reload_live_metadata()
            return

        logger.debug(
            "AssetListModel: linksUpdated for %s requires reloading view rooted at %s.",
            updated_root,
            album_root,
        )

        if self._rows:
            # ``_reload_live_metadata`` mutates cached rows in place so the view
            # reflects the latest Live Photo pairings without delay.
            self._reload_live_metadata()

        if self._loader_worker is not None:
            # ``start_load`` also cancels running workers, but we want to avoid
            # resetting the model twice.  Mark the reload intention and cancel the
            # worker from here so ``_on_loader_finished`` can honour the request
            # once the background thread exits.
            self._pending_reload = True
            if not self._loader_worker.cancelled:
                self._loader_worker.cancel()
            return

        if not self._pending_reload:
            QTimer.singleShot(0, self.start_load)

    def _links_update_targets_current_view(
        self, album_root: Path, updated_root: Path
    ) -> bool:
        """Return ``True`` when ``links.json`` updates should refresh the model.

        The method compares the normalised path of the dataset currently exposed
        by the model with the path for which the backend rebuilt ``links.json``.
        A refresh is required in two situations:

        * The backend updated ``links.json`` for the exact same root that feeds
          the model.
        * The model shows a library-wide view (for example "All Photos" or
          "Live Photos") and the backend refreshed ``links.json`` for an album
          living under that library root.

        Normalising via :func:`os.path.realpath` and :func:`os.path.normcase`
        ensures that comparisons remain stable across platforms and symbolic
        link setups where the same directory may be referenced through different
        aliases.
        """

        if album_root == updated_root:
            return True

        return self._is_descendant_path(updated_root, album_root)

    @staticmethod
    def _is_descendant_path(path: Path, candidate_root: Path) -> bool:
        """Return ``True`` when *path* is located under *candidate_root*.

        The helper treats equality as a positive match so callers can avoid
        special casing.  ``Path.parents`` yields every ancestor of *path*, making
        it a convenient way to check the relationship without manual string
        operations that could break across platforms.
        """

        if path == candidate_root:
            return True

        return candidate_root in path.parents

    @staticmethod
    def _normalise_for_compare(path: Path) -> Path:
        """Return a normalised ``Path`` suitable for cross-platform comparisons.

        ``Path.resolve`` is insufficient on its own because it preserves the
        original casing on case-insensitive filesystems.  Combining
        :func:`os.path.realpath` with :func:`os.path.normcase` yields a canonical
        representation that collapses symbolic links and performs the necessary
        case folding so that two references to the same directory compare equal
        regardless of how they were produced.
        """

        try:
            resolved = os.path.realpath(path)
        except OSError:
            resolved = str(path)
        return Path(os.path.normcase(resolved))

    def _reload_live_metadata(self) -> None:
        """Re-read ``links.json`` and update cached Live Photo roles."""

        if not self._album_root or not self._rows:
            return

        live_map = load_live_map(self._album_root)
        self._live_map = dict(live_map)

        updated_rows: List[int] = []
        album_root = self._normalise_path(self._album_root)

        for row_index, row in enumerate(self._rows):
            rel = str(row.get("rel", ""))
            if not rel:
                continue

            info = self._live_map.get(rel)
            new_is_live = False
            new_motion_rel: Optional[str] = None
            new_motion_abs: Optional[str] = None
            new_group_id: Optional[str] = None

            if isinstance(info, dict):
                group_id = info.get("id")
                if isinstance(group_id, str):
                    new_group_id = group_id
                elif group_id is not None:
                    new_group_id = str(group_id)

                if info.get("role") == "still":
                    motion_rel = info.get("motion")
                    if isinstance(motion_rel, str) and motion_rel:
                        new_motion_rel = motion_rel
                        try:
                            new_motion_abs = str((album_root / motion_rel).resolve())
                        except OSError:
                            # ``resolve`` can fail if the filesystem entry has
                            # just been created and metadata propagation is
                            # still underway.  Fall back to a best-effort
                            # absolute path so the UI can continue rendering a
                            # valid hyperlink.
                            new_motion_abs = str(album_root / motion_rel)
                        new_is_live = True

            previous_is_live = bool(row.get("is_live", False))
            previous_motion_rel = row.get("live_motion")
            previous_motion_abs = row.get("live_motion_abs")
            previous_group_id = row.get("live_group_id")

            if (
                previous_is_live == new_is_live
                and (previous_motion_rel or None) == new_motion_rel
                and (previous_motion_abs or None) == new_motion_abs
                and (previous_group_id or None) == new_group_id
            ):
                continue

            row["is_live"] = new_is_live
            row["live_motion"] = new_motion_rel
            row["live_motion_abs"] = new_motion_abs
            row["live_group_id"] = new_group_id
            updated_rows.append(row_index)

        if not updated_rows:
            return

        roles_to_refresh = [
            Roles.IS_LIVE,
            Roles.LIVE_GROUP_ID,
            Roles.LIVE_MOTION_REL,
            Roles.LIVE_MOTION_ABS,
        ]
        for row_index in updated_rows:
            model_index = self.index(row_index, 0)
            self.dataChanged.emit(model_index, model_index, roles_to_refresh)

    @staticmethod
    def _normalise_path(path: Path) -> Path:
        """Return a consistently resolved form of *path* for comparisons."""

        try:
            return path.resolve()
        except OSError:
            return path
