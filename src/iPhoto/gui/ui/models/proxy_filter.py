"""Filtering helpers for album asset views."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QSortFilterProxyModel, Qt

from .roles import Roles


class AssetFilterProxyModel(QSortFilterProxyModel):
    """Filter model that exposes convenience helpers for static collections."""

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._filter_mode: Optional[str] = None
        self._search_text: str = ""
        self._default_sort_role: int = int(Roles.DT)
        self._default_sort_order: Qt.SortOrder = Qt.SortOrder.DescendingOrder
        self._monitored_source: Optional[QAbstractItemModel] = None
        self.setDynamicSortFilter(True)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        # ``configure_default_sort`` applies the sort role and ensures the proxy
        # starts tracking chronological order immediately.  Sorting is
        # reapplied whenever the source model resets so background reloads
        # triggered by move/restore operations keep the UI consistent.
        self.configure_default_sort(Roles.DT, Qt.SortOrder.DescendingOrder)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_filter_mode(self, mode: Optional[str]) -> None:
        normalized = mode.casefold() if isinstance(mode, str) and mode else None
        if normalized == self._filter_mode:
            return
        self._filter_mode = normalized
        self.invalidateFilter()

    def filter_mode(self) -> Optional[str]:
        return self._filter_mode

    def set_search_text(self, text: str) -> None:
        normalized = text.strip().casefold()
        if normalized == self._search_text:
            return
        self._search_text = normalized
        self.invalidateFilter()

    def search_text(self) -> str:
        return self._search_text

    def set_filters(self, *, mode: Optional[str] = None, text: Optional[str] = None) -> None:
        changed = False
        if mode is not None and mode.casefold() != (self._filter_mode or ""):
            self._filter_mode = mode.casefold() if mode else None
            changed = True
        if text is not None and text.strip().casefold() != self._search_text:
            self._search_text = text.strip().casefold()
            changed = True
        if changed:
            self.invalidateFilter()

    def configure_default_sort(
        self,
        role: int | Roles,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        """Record *role* and *order* as the canonical sort configuration.

        The proxy falls back to the configured values whenever the underlying
        model resets.  This keeps the gallery views stable across background
        reloads such as the ones triggered by move or restore operations.
        """

        normalized_role = int(role)
        if (
            self._default_sort_role == normalized_role
            and self._default_sort_order == order
        ):
            self._reapply_default_sort()
            return
        self._default_sort_role = normalized_role
        self._default_sort_order = order
        self._reapply_default_sort()

    def apply_default_sort(self) -> None:
        """Reapply the stored default sort order to the current dataset."""

        self._reapply_default_sort()

    def setSourceModel(self, sourceModel: QAbstractItemModel | None) -> None:  # type: ignore[override]
        """Attach *sourceModel* while keeping default sort hooks in sync."""

        if self._monitored_source is not None:
            try:
                self._monitored_source.modelReset.disconnect(self._on_source_model_reset)
            except (TypeError, RuntimeError):  # pragma: no cover - Qt disconnect quirk
                pass
            try:
                self._monitored_source.layoutChanged.disconnect(
                    self._on_source_layout_changed
                )
            except (TypeError, RuntimeError):  # pragma: no cover - Qt disconnect quirk
                pass
        super().setSourceModel(sourceModel)
        self._monitored_source = sourceModel
        if sourceModel is not None:
            sourceModel.modelReset.connect(self._on_source_model_reset)
            sourceModel.layoutChanged.connect(self._on_source_layout_changed)
        self._reapply_default_sort()

    # ------------------------------------------------------------------
    # QSortFilterProxyModel API
    # ------------------------------------------------------------------
    def filterAcceptsRow(self, row: int, parent) -> bool:  # type: ignore[override]
        source = self.sourceModel()
        if source is None:
            return False
        index = source.index(row, 0, parent)
        if not index.isValid():
            return False
        if self._filter_mode == "videos" and not bool(index.data(Roles.IS_VIDEO)):
            return False
        if self._filter_mode == "live" and not bool(index.data(Roles.IS_LIVE)):
            return False
        if self._filter_mode == "favorites" and not bool(index.data(Roles.FEATURED)):
            return False
        if self._search_text:
            rel = index.data(Roles.REL)
            name = str(rel).casefold() if rel is not None else ""
            asset_id = index.data(Roles.ASSET_ID)
            identifier = str(asset_id).casefold() if asset_id is not None else ""
            if self._search_text not in name and self._search_text not in identifier:
                return False
        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # type: ignore[override]
        """Apply a timestamp-aware comparison when sorting by :data:`Roles.DT`."""

        if self.sortRole() == int(Roles.DT):
            left_value = self._coerce_timestamp(left.data(Roles.DT))
            right_value = self._coerce_timestamp(right.data(Roles.DT))
            if left_value == right_value:
                # Use the relative path as a deterministic tiebreaker so the
                # proxy order stays stable even when multiple assets share the
                # same timestamp.
                left_rel = str(left.data(Roles.REL) or "")
                right_rel = str(right.data(Roles.REL) or "")
                return left_rel < right_rel
            return left_value < right_value
        return super().lessThan(left, right)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _reapply_default_sort(self) -> None:
        """Apply the cached default sort settings to the proxy model."""

        self.setSortRole(self._default_sort_role)
        super().sort(0, self._default_sort_order)

    def _on_source_model_reset(self) -> None:
        """Reapply chronological sorting after the source model resets."""

        self._reapply_default_sort()

    def _on_source_layout_changed(self, *_args) -> None:
        """Ensure layout changes keep the proxy aligned with the default sort."""

        self._reapply_default_sort()

    @staticmethod
    def _coerce_timestamp(value: object) -> float:
        """Return a sortable timestamp for ``value``.

        ``index.jsonl`` stores capture times as ISO-8601 strings with a trailing
        ``Z``.  The helper normalises the representation and falls back to
        ``-inf`` for missing or unparsable values so assets without metadata sort
        to the end of descending views.
        """

        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime):
            stamp = value
        elif isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return float("-inf")
            if normalized.endswith("Z"):
                normalized = f"{normalized[:-1]}+00:00"
            try:
                stamp = datetime.fromisoformat(normalized)
            except ValueError:
                return float("-inf")
        else:
            return float("-inf")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        try:
            return stamp.timestamp()
        except OSError:  # pragma: no cover - out-of-range timestamp on platform
            return float("-inf")
