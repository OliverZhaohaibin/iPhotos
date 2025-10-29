"""Proxy model that applies filtering to :class:`AssetListModel`."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import Qt

if TYPE_CHECKING:  # pragma: no cover - import for type checking only
    from ...facade import AppFacade
from ..tasks.thumbnail_loader import ThumbnailLoader
from .asset_list_model import AssetListModel
from .proxy_filter import AssetFilterProxyModel
from .roles import Roles

__all__ = ["AssetModel", "Roles"]


class AssetModel(AssetFilterProxyModel):
    """Main entry point for asset data used by the widget views."""

    def __init__(self, facade: "AppFacade") -> None:
        super().__init__()
        self._list_model = facade.asset_list_model
        self.setSourceModel(self._list_model)
        # Ensure the main proxy always defaults to chronological ordering so the
        # aggregated collections (All Photos, Videos, Live Photos, Favorites)
        # surface the newest captures first even after background reloads.
        self.ensure_chronological_order()

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    def source_model(self) -> AssetListModel:
        return self._list_model

    def thumbnail_loader(self) -> ThumbnailLoader:
        return self._list_model.thumbnail_loader()

    # ------------------------------------------------------------------
    # Sorting helpers
    # ------------------------------------------------------------------
    def ensure_chronological_order(self) -> None:
        """Sort assets by capture timestamp with the newest entries first."""

        self.configure_default_sort(Roles.DT, Qt.SortOrder.DescendingOrder)

    # ------------------------------------------------------------------
    # Thumbnail prioritisation helpers
    # ------------------------------------------------------------------
    def prioritize_rows(self, first: int, last: int) -> None:
        """Map *first*→*last* proxy rows to the source model and prioritise them."""

        if last < 0 or self.rowCount() == 0:
            return

        if first > last:
            first, last = last, first

        first = max(first, 0)
        last = min(last, self.rowCount() - 1)
        if first > last:
            return

        map_to_source = self.mapToSource
        seen: set[int] = set()
        runs: list[tuple[int, int]] = []
        run_start: Optional[int] = None
        previous: Optional[int] = None

        for proxy_row in range(first, last + 1):
            proxy_index = self.index(proxy_row, 0)
            if not proxy_index.isValid():
                continue
            source_index = map_to_source(proxy_index)
            if not source_index.isValid():
                continue
            source_row = source_index.row()
            if source_row in seen:
                continue
            seen.add(source_row)
            if run_start is None:
                run_start = previous = source_row
                continue
            assert previous is not None
            if source_row == previous + 1:
                previous = source_row
            else:
                runs.append((run_start, previous))
                run_start = previous = source_row
        if run_start is not None and previous is not None:
            runs.append((run_start, previous))

        for start, end in runs:
            self._list_model.prioritize_rows(start, end)
