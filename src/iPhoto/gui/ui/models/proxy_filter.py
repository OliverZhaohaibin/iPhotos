"""Filtering helpers for album asset views."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSortFilterProxyModel, Qt

from .roles import Roles


class AssetFilterProxyModel(QSortFilterProxyModel):
    """Filter model that exposes convenience helpers for static collections."""

    def __init__(self, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._filter_mode: Optional[str] = None
        self._search_text: str = ""
        self.setDynamicSortFilter(True)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)

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
        if self._filter_mode and self._filter_mode.startswith("location:"):
            expected = self._filter_mode.partition(":")[2]
            location_raw = index.data(Roles.LOCATION)
            location_name = (
                str(location_raw).casefold() if location_raw is not None else ""
            )
            if expected and location_name != expected:
                return False
        if self._search_text:
            rel = index.data(Roles.REL)
            name = str(rel).casefold() if rel is not None else ""
            asset_id = index.data(Roles.ASSET_ID)
            identifier = str(asset_id).casefold() if asset_id is not None else ""
            if self._search_text not in name and self._search_text not in identifier:
                return False
        return True
