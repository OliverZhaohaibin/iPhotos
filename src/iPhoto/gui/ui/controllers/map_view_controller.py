"""Controller responsible for the Location map view."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject

from ....library.manager import GeotaggedAsset, LibraryManager
from ..media.playlist_controller import PlaylistController
from ..widgets.photo_map_view import PhotoMapView
from .view_controller import ViewController


class LocationMapController(QObject):
    """Load geotagged assets and keep the map view in sync with the library."""

    def __init__(
        self,
        library: LibraryManager,
        playlist: PlaylistController,
        view_controller: ViewController,
        map_view: PhotoMapView,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._library = library
        self._playlist = playlist
        self._view_controller = view_controller
        self._map_view = map_view
        self._cached_assets: list[GeotaggedAsset] = []
        self._is_visible = False

        self._map_view.assetActivated.connect(self._handle_asset_activated)

    def show_map_view(self) -> None:
        """Display the map view and refresh markers if necessary."""

        self._is_visible = True
        self._ensure_assets()
        self._view_controller.show_map_view()

    def shutdown(self) -> None:
        """Close the map view so its worker threads can exit cleanly."""

        # ``PhotoMapView`` owns a ``TileManager`` (running in a ``QThread``)
        # and a marker clustering worker.  Calling ``close()`` triggers the
        # widget's ``closeEvent`` hook which shuts both subsystems down.
        if self._map_view is not None:
            self._map_view.close()

    def hide_map_view(self) -> None:
        """Return to the standard gallery view."""

        if not self._is_visible:
            return
        self._is_visible = False
        self._view_controller.restore_default_gallery()

    def refresh_assets(self) -> None:
        """Reload the list of geotagged assets from the library."""

        root = self._library.root()
        if root is None:
            self._cached_assets = []
            self._map_view.clear()
            return
        assets = self._library.get_geotagged_assets()
        self._cached_assets = list(assets)
        self._map_view.set_assets(self._cached_assets, root)

    def handle_index_update(self, root: Path) -> None:
        """Refresh markers when the underlying index finishes updating."""

        if not self._is_visible:
            # Keep the cached list in sync so the next activation is immediate.
            self.refresh_assets()
            return
        if self._library.root() is None:
            self._map_view.clear()
            return
        self.refresh_assets()

    def is_visible(self) -> bool:
        """Return ``True`` when the map view is currently active."""

        return self._is_visible

    def _ensure_assets(self) -> None:
        root = self._library.root()
        if root is None:
            self._map_view.clear()
            return
        if not self._cached_assets:
            self.refresh_assets()
        else:
            self._map_view.set_assets(self._cached_assets, root)

    def _handle_asset_activated(self, rel: str) -> None:
        """Select *rel* in the playlist and transition to the detail view."""

        if not rel:
            return
        if self._playlist.set_current_by_relative(rel):
            self._view_controller.show_detail_view()


__all__ = ["LocationMapController"]
