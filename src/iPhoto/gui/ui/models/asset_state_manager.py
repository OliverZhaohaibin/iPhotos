"""State helpers extracted from :class:`AssetListModel`."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QModelIndex

if False:  # pragma: no cover - circular import guard
    from .asset_list_model import AssetListModel
    from .asset_cache_manager import AssetCacheManager


class AssetListStateManager:
    """Maintain row data and transient flags for the asset list model."""

    def __init__(self, model: "AssetListModel", cache_manager: "AssetCacheManager") -> None:
        """Initialise a state manager bound to *model* and *cache_manager*."""

        self._model = model
        self._cache = cache_manager
        self._rows: List[Dict[str, object]] = []
        self._row_lookup: Dict[str, int] = {}
        self._visible_rows: Set[int] = set()
        self._pending_virtual_moves: Dict[str, Tuple[int, str, bool]] = {}
        self._pending_row_removals: List[Tuple[int, Dict[str, object]]] = []
        self._pending_reload = False
        self._suppress_virtual_reload = False
        self._virtual_move_requires_revisit = False

    @property
    def rows(self) -> List[Dict[str, object]]:
        """Return the list of cached asset rows."""

        return self._rows

    @property
    def row_lookup(self) -> Dict[str, int]:
        """Return a mapping between ``rel`` paths and row indices."""

        return self._row_lookup

    @property
    def visible_rows(self) -> Set[int]:
        """Return the set of row indices currently prioritised for thumbnails."""

        return self._visible_rows

    def set_visible_rows(self, visible: Set[int]) -> None:
        """Replace the cached set of visible rows."""

        self._visible_rows = set(visible)

    def clear_visible_rows(self) -> None:
        """Forget the cached set of visible rows."""

        self._visible_rows.clear()

    def row_count(self) -> int:
        """Return the number of cached rows."""

        return len(self._rows)

    def set_rows(self, rows: List[Dict[str, object]]) -> None:
        """Replace the internal dataset with *rows* and rebuild lookups."""

        self._rows = rows
        self._row_lookup = {row["rel"]: index for index, row in enumerate(rows)}

    def clear_rows(self) -> None:
        """Remove all cached rows and transient move metadata."""

        self._rows = []
        self._row_lookup = {}
        self._visible_rows.clear()
        self._pending_virtual_moves.clear()
        self._pending_row_removals.clear()

    def rebuild_lookup(self) -> None:
        """Recompute the ``rel`` → index mapping after in-place row changes."""

        refreshed: Dict[str, int] = {}
        for index, row in enumerate(self._rows):
            rel_value = row.get("rel")
            if isinstance(rel_value, str) and rel_value:
                refreshed[Path(rel_value).as_posix()] = index
            elif isinstance(rel_value, Path):
                refreshed[rel_value.as_posix()] = index
            elif rel_value:
                refreshed[Path(str(rel_value)).as_posix()] = index
        self._row_lookup = refreshed

    @staticmethod
    def _normalise_key(value: Optional[str]) -> Optional[str]:
        """Return a POSIX-formatted representation of ``value`` when possible."""

        if not value:
            return None
        return Path(value).as_posix()

    def on_external_row_removed(self, position: int, rel_key: Optional[str]) -> None:
        """Adjust pending move bookkeeping after a row was removed externally."""

        if self._pending_virtual_moves:
            normalised_rel = self._normalise_key(rel_key)
            updated: Dict[str, Tuple[int, str, bool]] = {}
            for original_rel, (row_index, guessed_rel, was_removed) in (
                list(self._pending_virtual_moves.items())
            ):
                if row_index == position:
                    continue
                if normalised_rel is not None:
                    original_key = self._normalise_key(original_rel)
                    guessed_key = self._normalise_key(guessed_rel)
                    if normalised_rel in {original_key, guessed_key}:
                        continue
                adjusted_index = row_index - 1 if row_index > position else row_index
                if adjusted_index < 0:
                    continue
                updated[original_rel] = (adjusted_index, guessed_rel, was_removed)
            self._pending_virtual_moves = updated

        if self._pending_row_removals:
            self._pending_row_removals.clear()

    def on_external_row_inserted(self, position: int) -> None:
        """Shift pending move bookkeeping after a row was inserted externally."""

        if self._pending_virtual_moves:
            adjusted: Dict[str, Tuple[int, str, bool]] = {}
            for original_rel, (row_index, guessed_rel, was_removed) in (
                list(self._pending_virtual_moves.items())
            ):
                if row_index >= position:
                    adjusted_index = row_index + 1
                else:
                    adjusted_index = row_index
                adjusted[original_rel] = (adjusted_index, guessed_rel, was_removed)
            self._pending_virtual_moves = adjusted

        if self._pending_row_removals:
            self._pending_row_removals.clear()

    def append_chunk(self, chunk: List[Dict[str, object]]) -> Tuple[int, int]:
        """Extend the dataset with *chunk* and return the inserted row range."""

        start_row = len(self._rows)
        self._rows.extend(chunk)
        for offset, row_data in enumerate(chunk):
            self._row_lookup[row_data["rel"]] = start_row + offset
        return start_row, start_row + len(chunk) - 1

    def active_rel_keys(self) -> Set[str]:
        """Return the current set of ``rel`` keys."""

        return set(self._row_lookup.keys())

    def mark_reload_pending(self) -> None:
        """Record that a loader restart should occur once the current run ends."""

        self._pending_reload = True

    def clear_reload_pending(self) -> None:
        """Clear the pending reload flag."""

        self._pending_reload = False

    def has_pending_reload(self) -> bool:
        """Return ``True`` when a reload should be scheduled."""

        return self._pending_reload

    def consume_pending_reload(self, album_root: Optional[Path], root: Path) -> bool:
        """Consume the pending reload flag and report whether ``root`` matches."""

        should_restart = bool(self._pending_reload and album_root and root == album_root)
        self._pending_reload = False
        return should_restart

    def suppress_virtual_reload(self) -> bool:
        """Return ``True`` when aggregate views should suppress reloads."""

        return self._suppress_virtual_reload

    def set_virtual_reload_suppressed(self, suppressed: bool) -> None:
        """Update whether aggregate views should ignore reload notifications."""

        self._suppress_virtual_reload = suppressed

    def virtual_move_requires_revisit(self) -> bool:
        """Return ``True`` if the UI must be revisited after a virtual move."""

        return self._virtual_move_requires_revisit

    def set_virtual_move_requires_revisit(self, value: bool) -> None:
        """Toggle the flag tracking whether a revisit is required."""

        self._virtual_move_requires_revisit = value

    def has_pending_move_placeholders(self) -> bool:
        """Return ``True`` when optimistic move updates are awaiting results."""

        return bool(self._pending_virtual_moves or self._pending_row_removals)

    def remove_rows(self, indexes: List[QModelIndex]) -> None:
        """Remove rows referenced by *indexes*, tolerating proxy models."""

        source_rows: Set[int] = set()
        for proxy_index in indexes:
            if not proxy_index.isValid() or proxy_index.column() != 0:
                continue
            current_index = proxy_index
            model = current_index.model()
            while model is not self._model and hasattr(model, "mapToSource"):
                mapped_index = model.mapToSource(current_index)
                if not mapped_index.isValid():
                    current_index = QModelIndex()
                    break
                current_index = mapped_index
                model = current_index.model()
            if not current_index.isValid() or model is not self._model:
                continue
            row_number = current_index.row()
            if 0 <= row_number < len(self._rows):
                source_rows.add(row_number)

        if not source_rows:
            return

        for row in sorted(source_rows, reverse=True):
            row_data = self._rows[row]
            rel_key = str(row_data["rel"])
            abs_key = str(row_data.get("abs")) if row_data.get("abs") else ""
            self._model.beginRemoveRows(QModelIndex(), row, row)
            self._rows.pop(row)
            self._model.endRemoveRows()
            self._row_lookup.pop(rel_key, None)
            self._cache.remove_thumbnail(rel_key)
            self._cache.remove_placeholder(rel_key)
            if abs_key:
                self._cache.stash_recently_removed(abs_key, row_data)

        self._row_lookup = {row["rel"]: index for index, row in enumerate(self._rows)}
        self._cache.clear_placeholders()
        self._visible_rows.clear()

    def update_rows_for_move(
        self,
        rels: List[str],
        destination_root: Path,
        album_root: Optional[Path],
        *,
        is_source_main_view: bool = False,
    ) -> List[int]:
        """Apply optimistic UI updates when a move operation is queued."""

        if not album_root or not rels:
            return []

        album_root_resolved = self._safe_resolve(album_root)

        if not is_source_main_view:
            rows_to_remove: List[int] = []
            for original_rel in {Path(rel).as_posix() for rel in rels}:
                row_index = self._row_lookup.get(original_rel)
                if row_index is None:
                    continue
                rows_to_remove.append(row_index)

            if not rows_to_remove:
                return []

            for row_index in sorted(set(rows_to_remove), reverse=True):
                if not (0 <= row_index < len(self._rows)):
                    continue

                row_snapshot = dict(self._rows[row_index])
                rel_key = str(row_snapshot.get("rel", ""))
                abs_key = str(row_snapshot.get("abs", "")) if row_snapshot.get("abs") else ""

                self._pending_row_removals.append((row_index, row_snapshot))

                self._model.beginRemoveRows(QModelIndex(), row_index, row_index)
                self._rows.pop(row_index)
                self._model.endRemoveRows()

                if rel_key:
                    self._row_lookup.pop(rel_key, None)
                    self._cache.remove_thumbnail(rel_key)
                    self._cache.remove_placeholder(rel_key)
                if abs_key:
                    self._cache.stash_recently_removed(abs_key, row_snapshot)

            self._row_lookup = {row["rel"]: index for index, row in enumerate(self._rows)}
            self._cache.clear_placeholders()
            self._visible_rows.clear()
            self._suppress_virtual_reload = True
            return []

        try:
            destination_resolved = self._safe_resolve(destination_root)
            dest_prefix = destination_resolved.relative_to(album_root_resolved)
        except OSError:
            return []
        except ValueError:
            return []

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
            guessed_abs = destination_resolved / file_name

            self._row_lookup.pop(original_rel, None)
            self._row_lookup[guessed_rel] = row_index
            self._cache.move_thumbnail(original_rel, guessed_rel)
            self._cache.move_placeholder(original_rel, guessed_rel)

            row_data["rel"] = guessed_rel
            row_data["abs"] = str(guessed_abs)
            self._pending_virtual_moves[original_rel] = (row_index, guessed_rel, False)
            changed_rows.append(row_index)

        if changed_rows:
            self._suppress_virtual_reload = True
            self._virtual_move_requires_revisit = True

        return changed_rows

    def finalise_move_results(
        self,
        moves: List[Tuple[Path, Path]],
        album_root: Optional[Path],
    ) -> List[int]:
        """Reconcile optimistic move updates with the worker results."""

        if not album_root or not moves:
            return []

        album_root_resolved = self._safe_resolve(album_root)
        updated_rows: List[int] = []

        for original_path, target_path in moves:
            try:
                original_rel = self._safe_resolve(original_path).relative_to(album_root_resolved).as_posix()
            except ValueError:
                continue

            pending = self._pending_virtual_moves.pop(original_rel, None)
            if pending is None:
                continue

            row_index, guessed_rel, was_removed = pending
            if not (0 <= row_index < len(self._rows)):
                if guessed_rel:
                    self._row_lookup.pop(guessed_rel, None)
                    self._cache.remove_thumbnail(guessed_rel)
                    self._cache.remove_placeholder(guessed_rel)
                continue
            row_data = self._rows[row_index]

            try:
                final_rel = self._safe_resolve(target_path).relative_to(album_root_resolved).as_posix()
            except ValueError:
                final_rel = guessed_rel

            if not was_removed:
                self._row_lookup.pop(guessed_rel, None)
                self._row_lookup[final_rel] = row_index
                self._cache.move_thumbnail(guessed_rel, final_rel)
                self._cache.move_placeholder(guessed_rel, final_rel)

            row_data["rel"] = final_rel
            row_data["abs"] = str(self._safe_resolve(target_path))
            updated_rows.append(row_index)

        if self._pending_row_removals:
            self._pending_row_removals.clear()

        return updated_rows

    def rollback_pending_moves(self, album_root: Optional[Path]) -> List[int]:
        """Restore original metadata for moves that failed or were cancelled."""

        if not album_root:
            self._pending_virtual_moves.clear()
            self._pending_row_removals.clear()
            self._suppress_virtual_reload = False
            self._virtual_move_requires_revisit = False
            return []

        album_root_resolved = self._safe_resolve(album_root)
        to_restore = list(self._pending_virtual_moves.items())
        self._pending_virtual_moves.clear()

        restored_rows: List[int] = []
        for original_rel, (row_index, guessed_rel, was_removed) in to_restore:
            row_data = self._rows[row_index]
            absolute = self._safe_resolve(album_root_resolved / original_rel)

            if not was_removed:
                self._row_lookup.pop(guessed_rel, None)
                self._row_lookup[original_rel] = row_index
                self._cache.move_thumbnail(guessed_rel, original_rel)
                self._cache.move_placeholder(guessed_rel, original_rel)

            row_data["rel"] = original_rel
            row_data["abs"] = str(absolute)
            restored_rows.append(row_index)

        if self._pending_row_removals:
            for row_index, row_data in sorted(self._pending_row_removals, key=lambda entry: entry[0]):
                insert_at = min(max(row_index, 0), len(self._rows))
                self._model.beginInsertRows(QModelIndex(), insert_at, insert_at)
                restored = dict(row_data)
                self._rows.insert(insert_at, restored)
                self._model.endInsertRows()
                abs_key = str(restored.get("abs", "")) if restored.get("abs") else ""
                if abs_key:
                    self._cache.remove_recently_removed(abs_key)
            self._pending_row_removals.clear()
            self._row_lookup = {row["rel"]: index for index, row in enumerate(self._rows)}
            self._cache.clear_all_thumbnails()
            self._cache.clear_placeholders()
            self._visible_rows.clear()

        self._suppress_virtual_reload = False
        self._virtual_move_requires_revisit = False

        return restored_rows

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        """Resolve *path* while tolerating filesystem races."""

        try:
            return path.resolve()
        except OSError:
            return path
