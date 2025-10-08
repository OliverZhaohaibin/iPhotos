"""Proxy model that applies filtering to :class:`AssetListModel`."""

from __future__ import annotations

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
