"""List model combining ``index.jsonl`` and ``links.json`` data."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSize,
    Qt,
    Signal,
    Slot,
    QTimer,
)
from PySide6.QtGui import QPixmap

from ..tasks.thumbnail_loader import ThumbnailLoader
from .asset_cache_manager import AssetCacheManager
from .asset_data_loader import AssetDataLoader
from .asset_state_manager import AssetListStateManager
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
        self._thumb_size = QSize(192, 192)
        self._cache_manager = AssetCacheManager(self._thumb_size, self)
        self._cache_manager.thumbnailReady.connect(self._on_thumb_ready)
        self._data_loader = AssetDataLoader(self)
        self._data_loader.chunkReady.connect(self._on_loader_chunk_ready)
        self._data_loader.loadProgress.connect(self._on_loader_progress)
        self._data_loader.loadFinished.connect(self._on_loader_finished)
        self._data_loader.error.connect(self._on_loader_error)
        self._state_manager = AssetListStateManager(self, self._cache_manager)
        self._cache_manager.set_recently_removed_limit(256)
        # ``_pending_rows`` accumulates worker results while a background load is
        # in flight.  Once :meth:`_on_loader_finished` fires we swap the buffered
        # snapshot into the model in a single reset so aggregate views only see
        # one visual refresh instead of flickering through multiple incremental
        # updates.
        self._pending_rows: List[Dict[str, object]] = []
        self._pending_loader_root: Optional[Path] = None

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

        rows = self._state_manager.rows
        if not rows:
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
                row_index = self._state_manager.row_lookup.get(rel_key)
                if row_index is not None and 0 <= row_index < len(rows):
                    return rows[row_index]

        normalized_str = str(normalized_path)
        for row in rows:
            if str(row.get("abs")) == normalized_str:
                return row
        # Fall back to the recently removed cache so operations triggered right
        # after an optimistic removal can still access metadata that is no
        # longer present in the live dataset.  The cache mirrors the structure
        # of the active rows, therefore callers can interact with the returned
        # dictionary exactly as if the row were still part of the model.
        cached = self._cache_manager.recently_removed(normalized_str)
        if cached is not None:
            return cached
        return None

    def remove_rows(self, indexes: list[QModelIndex]) -> None:
        """Remove assets referenced by *indexes*, tolerating proxy selections."""

        self._state_manager.remove_rows(indexes)

    def update_rows_for_move(
        self,
        rels: list[str],
        destination_root: Path,
        *,
        is_source_main_view: bool = False,
    ) -> None:
        """Apply optimistic UI updates when a move operation is queued."""

        if not self._album_root:
            return

        changed_rows = self._state_manager.update_rows_for_move(
            rels,
            destination_root,
            self._album_root,
            is_source_main_view=is_source_main_view,
        )

        for row in changed_rows:
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index,
                model_index,
                [Roles.REL, Roles.ABS, Qt.DecorationRole],
            )

    def finalise_move_results(self, moves: List[Tuple[Path, Path]]) -> None:
        """Reconcile optimistic move updates with the worker results."""

        updated_rows = self._state_manager.finalise_move_results(moves, self._album_root)

        for row in updated_rows:
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index,
                model_index,
                [Roles.REL, Roles.ABS, Qt.DecorationRole],
            )

    def rollback_pending_moves(self) -> None:
        """Restore original metadata for moves that failed or were cancelled."""

        restored_rows = self._state_manager.rollback_pending_moves(self._album_root)

        for row in restored_rows:
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index,
                model_index,
                [Roles.REL, Roles.ABS, Qt.DecorationRole],
            )

    def has_pending_move_placeholders(self) -> bool:
        """Return ``True`` when optimistic move updates are awaiting results."""

        return self._state_manager.has_pending_move_placeholders()
    def populate_from_cache(self, *, max_index_bytes: int = 512 * 1024) -> bool:
        """Synchronously load cached index data when the file is small."""

        if not self._album_root:
            return False
        if self._data_loader.is_running():
            return False

        root = self._album_root
        manifest = self._facade.current_album.manifest if self._facade.current_album else {}
        featured = manifest.get("featured", []) or []
        live_map = load_live_map(root)
        self._cache_manager.set_live_map(live_map)

        # ``AssetDataLoader.populate_from_cache`` computes the rows immediately yet
        # defers all signal emission to the next event-loop iteration.  This mirrors
        # the asynchronous worker behaviour so ``QSignalSpy`` and other listeners
        # attached right after :meth:`AppFacade.open_album` still observe
        # ``loadFinished`` notifications.
        result = self._data_loader.populate_from_cache(
            root,
            featured,
            self._cache_manager.live_map_snapshot(),
            max_index_bytes=max_index_bytes,
        )
        if result is None:
            return False

        rows, _ = result

        self._pending_rows = []
        self._pending_loader_root = None

        self.beginResetModel()
        self._state_manager.set_rows(rows)
        self.endResetModel()

        self._cache_manager.reset_caches_for_new_rows(rows)
        self._state_manager.clear_reload_pending()

        return True

    # ------------------------------------------------------------------
    # Qt model implementation
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex | None = None) -> int:  # type: ignore[override]
        if parent is not None and parent.isValid():  # pragma: no cover - tree fallback
            return 0
        return self._state_manager.row_count()

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # type: ignore[override]
        rows = self._state_manager.rows
        if not index.isValid() or not (0 <= index.row() < len(rows)):
            return None
        row = rows[index.row()]
        if role == Qt.DisplayRole:
            return ""
        if role == Qt.DecorationRole:
            return self._cache_manager.resolve_thumbnail(row, ThumbnailLoader.Priority.NORMAL)
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
        rows = self._state_manager.rows
        if not index.isValid() or not (0 <= index.row() < len(rows)):
            return False
        if role != Roles.IS_CURRENT:
            return super().setData(index, value, role)

        normalized = bool(value)
        row = rows[index.row()]
        if bool(row.get("is_current", False)) == normalized:
            return True
        row["is_current"] = normalized
        self.dataChanged.emit(index, index, [Roles.IS_CURRENT])
        return True

    def thumbnail_loader(self) -> ThumbnailLoader:
        return self._cache_manager.thumbnail_loader()

    # ------------------------------------------------------------------
    # Facade callbacks
    # ------------------------------------------------------------------
    def prepare_for_album(self, root: Path) -> None:
        """Reset internal state so *root* becomes the active album."""

        if self._data_loader.is_running():
            self._data_loader.cancel()
        self._state_manager.clear_reload_pending()
        self._album_root = root
        self._cache_manager.reset_for_album(root)
        self.beginResetModel()
        self._state_manager.clear_rows()
        self.endResetModel()
        self._cache_manager.clear_recently_removed()
        self._state_manager.set_virtual_reload_suppressed(False)
        self._state_manager.set_virtual_move_requires_revisit(False)
        self._pending_rows = []
        self._pending_loader_root = None

    def update_featured_status(self, rel: str, is_featured: bool) -> None:
        """Update the cached ``featured`` flag for the asset identified by *rel*."""

        rel_key = str(rel)
        row_index = self._state_manager.row_lookup.get(rel_key)
        rows = self._state_manager.rows
        if row_index is None or not (0 <= row_index < len(rows)):
            return

        row = rows[row_index]
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
        if self._data_loader.is_running():
            self._data_loader.cancel()
            self._state_manager.mark_reload_pending()
            return

        manifest = self._facade.current_album.manifest if self._facade.current_album else {}
        featured = manifest.get("featured", []) or []

        live_map = load_live_map(self._album_root)
        self._cache_manager.set_live_map(live_map)

        # Remember which album root is being populated so chunk handlers can
        # accumulate results while the worker traverses the filesystem.
        self._pending_rows = []
        self._pending_loader_root = self._album_root

        try:
            self._data_loader.start(self._album_root, featured, live_map)
        except RuntimeError:
            self._state_manager.mark_reload_pending()
            self._pending_rows = []
            self._pending_loader_root = None
            return

        self._state_manager.clear_reload_pending()

    def _on_loader_chunk_ready(self, root: Path, chunk: List[Dict[str, object]]) -> None:
        if (
            not self._album_root
            or root != self._album_root
            or not chunk
            or self._pending_loader_root != self._album_root
        ):
            return

        # Buffer worker rows so the view can be refreshed exactly once when the
        # load completes instead of growing incrementally and triggering a full
        # resort after every sub-album traversal.
        self._pending_rows.extend(chunk)

    def _on_loader_progress(self, root: Path, current: int, total: int) -> None:
        if not self._album_root or root != self._album_root:
            return
        self.loadProgress.emit(root, current, total)

    def _on_loader_finished(self, root: Path, success: bool) -> None:
        if not self._album_root or root != self._album_root:
            should_restart = self._state_manager.consume_pending_reload(self._album_root, root)
            if should_restart:
                QTimer.singleShot(0, self.start_load)
            return

        if success and self._pending_loader_root == self._album_root:
            rows = list(self._pending_rows)
            self.beginResetModel()
            self._state_manager.set_rows(rows)
            self.endResetModel()
            self._cache_manager.reset_caches_for_new_rows(rows)
            self._cache_manager.clear_recently_removed()

        self.loadFinished.emit(root, success)

        self._pending_rows = []
        self._pending_loader_root = None

        should_restart = self._state_manager.consume_pending_reload(self._album_root, root)
        if should_restart:
            QTimer.singleShot(0, self.start_load)

    def _on_loader_error(self, root: Path, message: str) -> None:
        if not self._album_root or root != self._album_root:
            should_restart = self._state_manager.consume_pending_reload(self._album_root, root)
            self.loadFinished.emit(root, False)
            if should_restart:
                QTimer.singleShot(0, self.start_load)
            return

        self._facade.errorRaised.emit(message)
        self.loadFinished.emit(root, False)

        self._pending_rows = []
        self._pending_loader_root = None

        should_restart = self._state_manager.consume_pending_reload(self._album_root, root)
        if should_restart:
            QTimer.singleShot(0, self.start_load)

    # ------------------------------------------------------------------
    # Thumbnail helpers
    # ------------------------------------------------------------------
    def prioritize_rows(self, first: int, last: int) -> None:
        """Request high-priority thumbnails for the inclusive range *first*→*last*."""

        rows = self._state_manager.rows
        if not rows:
            self._state_manager.clear_visible_rows()
            return

        if first > last:
            first, last = last, first

        first = max(first, 0)
        last = min(last, len(rows) - 1)
        if first > last:
            self._state_manager.clear_visible_rows()
            return

        requested = set(range(first, last + 1))
        if not requested:
            self._state_manager.clear_visible_rows()
            return

        uncached = {
            row
            for row in requested
            if self._cache_manager.thumbnail_for(str(rows[row]["rel"])) is None
        }
        if not uncached:
            self._state_manager.set_visible_rows(requested)
            return
        if uncached.issubset(self._state_manager.visible_rows):
            self._state_manager.set_visible_rows(requested)
            return

        self._state_manager.set_visible_rows(requested)
        for row in range(first, last + 1):
            if row not in uncached:
                continue
            row_data = rows[row]
            self._cache_manager.resolve_thumbnail(
                row_data, ThumbnailLoader.Priority.VISIBLE
            )

    def _on_thumb_ready(self, root: Path, rel: str, pixmap: QPixmap) -> None:
        if not self._album_root or root != self._album_root:
            return
        index = self._state_manager.row_lookup.get(rel)
        if index is None:
            return
        model_index = self.index(index, 0)
        self.dataChanged.emit(model_index, model_index, [Qt.DecorationRole])

    @Slot(Path)
    def handle_links_updated(self, root: Path) -> None:
        """React to :mod:`links.json` refreshes triggered by the backend."""

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

        if self._state_manager.suppress_virtual_reload():
            if self._state_manager.virtual_move_requires_revisit():
                logger.debug(
                    "AssetListModel: holding reload for %s until the aggregate view is reopened.",
                    updated_root,
                )
                return

            logger.debug(
                "AssetListModel: finishing temporary suppression for %s after non-aggregate move.",
                updated_root,
            )
            self._state_manager.set_virtual_reload_suppressed(False)
            if self._state_manager.rows:
                self._reload_live_metadata()
            return

        logger.debug(
            "AssetListModel: linksUpdated for %s requires reloading view rooted at %s.",
            updated_root,
            album_root,
        )

        if self._state_manager.rows:
            self._reload_live_metadata()

        if self._data_loader.is_running():
            self._state_manager.mark_reload_pending()
            self._data_loader.cancel()
            return

        if not self._state_manager.has_pending_reload():
            self._state_manager.mark_reload_pending()
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

        rows = self._state_manager.rows
        if not self._album_root or not rows:
            return

        updated_rows = self._cache_manager.reload_live_metadata(rows)
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
