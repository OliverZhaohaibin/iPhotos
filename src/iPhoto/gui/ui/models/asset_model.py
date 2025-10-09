"""Proxy model that applies filtering to :class:`AssetListModel`."""

from __future__ import annotations

from typing import Iterable

from ...facade import AppFacade
from ..tasks.thumbnail_loader import ThumbnailLoader
from .asset_list_model import AssetListModel
from .proxy_filter import AssetFilterProxyModel
from .roles import Roles

__all__ = ["AssetModel", "Roles"]


class AssetModel(AssetFilterProxyModel):
    """Main entry point for asset data used by the widget views."""

    def __init__(self, facade: AppFacade) -> None:
        super().__init__()
        self._list_model = facade.asset_list_model
        self.setSourceModel(self._list_model)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    def source_model(self) -> AssetListModel:
        return self._list_model

    def thumbnail_loader(self) -> ThumbnailLoader:
        return self._list_model.thumbnail_loader()

    # ------------------------------------------------------------------
    # Thumbnail prioritisation helpers
    # ------------------------------------------------------------------
    def prioritize_rows(self, rows: Iterable[int]) -> None:
        """Forward *rows* from the proxy space to the source model."""

        proxy_rows = list(rows)
        if not proxy_rows:
            return

        source_rows: list[int] = []
        map_to_source = self.mapToSource
        for row in proxy_rows:
            if row < 0:
                continue
            proxy_index = self.index(row, 0)
            if not proxy_index.isValid():
                continue
            source_index = map_to_source(proxy_index)
            if not source_index.isValid():
                continue
            source_rows.append(source_index.row())

        if not source_rows:
            return

        self._list_model.prioritize_rows(source_rows)
