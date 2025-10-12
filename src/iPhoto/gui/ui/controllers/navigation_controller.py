"""Coordinate album navigation and sidebar selections."""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING

from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QLabel, QStatusBar

# Support both package-style and legacy ``iPhotos.src`` imports during GUI
# bootstrap.
try:  # pragma: no cover - path-sensitive import
    from ...appctx import AppContext
except ImportError:  # pragma: no cover - executed in script mode
    from iPhotos.src.iPhoto.appctx import AppContext
from ...facade import AppFacade
from ..models.asset_model import AssetModel, Roles
from ..widgets.album_sidebar import AlbumSidebar
from .dialog_controller import DialogController
from .view_controller import ViewController

if TYPE_CHECKING:  # pragma: no cover - import used for type checking only
    from ..widgets.map_view import MapView


class NavigationController:
    """Handle opening albums and switching between static collections."""

    def __init__(
        self,
        context: AppContext,
        facade: AppFacade,
        asset_model: AssetModel,
        sidebar: AlbumSidebar,
        album_label: QLabel,
        status_bar: QStatusBar,
        dialog: DialogController,
        view_controller: ViewController,
        map_view: "MapView" | None,
    ) -> None:
        self._context = context
        self._facade = facade
        self._asset_model = asset_model
        self._sidebar = sidebar
        self._album_label = album_label
        self._status = status_bar
        self._dialog = dialog
        self._view_controller = view_controller
        self._map_view = map_view
        self._static_selection: Optional[str] = None
        self._active_location_name: Optional[str] = None
        # ``_last_open_was_refresh`` records whether ``open_album`` most recently
        # reissued the currently open album.  When ``True`` the main window can
        # keep the detail pane visible rather than reverting to the gallery.
        self._last_open_was_refresh: bool = False

        if self._map_view is not None:
            self._map_view.clusterClicked.connect(self._handle_cluster_clicked)

        # Keep the map view in sync with the asset model so that late-arriving
        # rows (for example while the background loader is still scanning) are
        # reflected without requiring manual refreshes.
        self._asset_model.modelReset.connect(self._handle_asset_model_reset)
        self._asset_model.rowsInserted.connect(self._handle_asset_model_rows_changed)
        self._asset_model.rowsRemoved.connect(self._handle_asset_model_rows_changed)

    # ------------------------------------------------------------------
    # Album management
    # ------------------------------------------------------------------
    def open_album(self, path: Path) -> None:
        # ``QFileSystemWatcher`` refreshes, library tree rebuilds and other
        # background activities occasionally reissue ``open_album`` for the
        # album the user is already browsing.  Those calls should be treated as
        # passive refreshes so the detail pane remains visible instead of
        # bouncing back to the gallery.  Compare the requested path with the
        # active album before touching any UI state so we can preserve the
        # current presentation when appropriate.
        target_root = path.resolve()
        current_root = (
            self._facade.current_album.root.resolve()
            if self._facade.current_album is not None
            else None
        )
        is_same_album = current_root == target_root

        # Static collections ("All Photos", "Favorites", etc.) deliberately
        # re-use the library root, so only treat the invocation as a refresh
        # when no static node is active.  This keeps virtual collections using
        # their gallery-first behaviour while allowing genuine album reloads to
        # bypass the gallery reset.
        is_refresh = bool(is_same_album and self._static_selection is None)
        self._last_open_was_refresh = is_refresh

        if is_refresh:
            # The album is already open and the caller is simply synchronising
            # sidebar state (for example after a manifest edit triggered by the
            # favorites button).  Returning early prevents a redundant call to
            # :meth:`AppFacade.open_album`, which would otherwise reset the
            # asset model, clear the playlist selection and bounce the detail
            # pane back to its placeholder.  The existing model already reflects
            # the manifest change via targeted data updates, so there is nothing
            # further to do.
            return

        self._static_selection = None
        self._active_location_name = None
        self._asset_model.set_filter_mode(None)
        # Present the gallery grid when navigating to a different album so the
        # UI avoids showing a stale detail pane while the model loads.
        self._view_controller.show_gallery_view()

        album = self._facade.open_album(path)
        if album is not None:
            self._context.remember_album(album.root)

    def handle_album_opened(self, root: Path) -> None:
        library_root = self._context.library.root()
        if self._static_selection and library_root == root:
            title = self._static_selection
            self._sidebar.select_static_node(self._static_selection)
        else:
            title = (
                self._facade.current_album.manifest.get("title")
                if self._facade.current_album
                else root.name
            )
            self._sidebar.select_path(root)
            self._static_selection = None
            self._asset_model.set_filter_mode(None)
        self._album_label.setText(f"{title} — {root}")
        self.update_status()

    # ------------------------------------------------------------------
    # Static collections
    # ------------------------------------------------------------------
    def open_all_photos(self) -> None:
        self.open_static_collection(AlbumSidebar.ALL_PHOTOS_TITLE, None)

    def open_static_node(self, title: str) -> None:
        if title.casefold() == "locations":
            self.open_locations_collection(title)
            return
        mapping = {
            "videos": "videos",
            "live photos": "live",
            "favorites": "favorites",
        }
        key = title.casefold()
        mode = mapping.get(key, None)
        self.open_static_collection(title, mode)

    def open_static_collection(self, title: str, filter_mode: Optional[str]) -> None:
        root = self._context.library.root()
        if root is None:
            self._dialog.bind_library_dialog()
            return
        # Reset the detail pane whenever a static collection (All Photos,
        # Favorites, etc.) is opened so the UI consistently shows the grid as
        # its entry point for that virtual album.
        self._view_controller.show_gallery_view()
        self._asset_model.set_filter_mode(filter_mode)
        self._static_selection = title
        self._active_location_name = None
        album = self._facade.open_album(root)
        if album is None:
            self._static_selection = None
            self._asset_model.set_filter_mode(None)
            return
        album.manifest = {**album.manifest, "title": title}

    def open_locations_collection(self, title: str) -> None:
        """Open the virtual "Locations" collection and present the map view."""

        root = self._context.library.root()
        if root is None:
            self._dialog.bind_library_dialog()
            return

        # Reset state so that location-specific filters do not leak between
        # sessions.  The gallery will be filtered when an individual cluster is
        # clicked, but the overview map should always start with the full data
        # set.
        self._asset_model.set_filter_mode(None)
        self._static_selection = title
        self._active_location_name = None

        self._view_controller.show_map_view()

        album = self._facade.open_album(root)
        if album is None:
            self._static_selection = None
            return
        album.manifest = {**album.manifest, "title": title}

        self._album_label.setText(f"{title} — {root}")

        clusters = self._build_location_clusters()
        if self._map_view is not None:
            self._map_view.set_photo_clusters(clusters)
        self._status.showMessage(self._format_location_status(clusters))

    def consume_last_open_refresh(self) -> bool:
        """Return ``True`` if the previous :meth:`open_album` was a refresh."""

        was_refresh = self._last_open_was_refresh
        self._last_open_was_refresh = False
        return was_refresh

    # ------------------------------------------------------------------
    # Location map helpers
    # ------------------------------------------------------------------
    def _format_location_status(self, clusters: Iterable[Dict[str, object]]) -> str:
        """Return a concise status message summarising *clusters*."""

        cluster_list = list(clusters)
        location_count = len(cluster_list)
        asset_total = 0
        for entry in cluster_list:
            try:
                asset_total += int(entry.get("count", 0))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue

        if location_count == 1:
            location_part = "1 location"
        else:
            location_part = f"{location_count} locations"

        if asset_total == 1:
            asset_part = "1 photo"
        else:
            asset_part = f"{asset_total} photos"

        return f"{location_part} covering {asset_part} with GPS metadata"

    def _build_location_clusters(self) -> List[Dict[str, object]]:
        """Aggregate model rows into location clusters suitable for the map."""

        source_model = self._asset_model.source_model()
        row_count = source_model.rowCount()
        grouped: Dict[str, List[Tuple[QModelIndex, float, float]]] = defaultdict(list)

        for row in range(row_count):
            index = source_model.index(row, 0)
            if not index.isValid():
                continue

            location_raw = index.data(Roles.LOCATION)
            if not isinstance(location_raw, str):
                continue
            location_name = location_raw.strip()
            if not location_name:
                continue

            gps_raw = index.data(Roles.GPS)
            latitude, longitude = self._extract_coordinates(gps_raw)
            if latitude is None or longitude is None:
                continue

            grouped[location_name].append((index, latitude, longitude))

        clusters: List[Dict[str, object]] = []
        for location_name in sorted(grouped.keys(), key=str.casefold):
            entries = grouped[location_name]
            if not entries:
                continue
            first_index, latitude, longitude = entries[0]
            thumb_raw = first_index.data(Roles.ABS)
            thumbnail_url = self._resolve_thumbnail_url(thumb_raw)
            clusters.append(
                {
                    "location_name": location_name,
                    "count": len(entries),
                    "lat": latitude,
                    "lon": longitude,
                    "thumbnail_url": thumbnail_url,
                }
            )
        return clusters

    @staticmethod
    def _coerce_float(value: object) -> Optional[float]:
        """Best-effort conversion of *value* to a finite ``float``."""

        try:
            candidate = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(candidate) or math.isinf(candidate):
            return None
        return candidate

    def _extract_coordinates(self, gps_raw: object) -> Tuple[Optional[float], Optional[float]]:
        """Normalize latitude/longitude from the raw GPS payload."""

        if not isinstance(gps_raw, dict):
            return None, None
        latitude = self._coerce_float(gps_raw.get("lat"))
        longitude = self._coerce_float(gps_raw.get("lon"))
        return latitude, longitude

    @staticmethod
    def _resolve_thumbnail_url(path_value: object) -> str:
        """Convert *path_value* to a URL that the map view can load."""

        if isinstance(path_value, Path):
            candidate = path_value
        else:
            try:
                candidate = Path(str(path_value))
            except (TypeError, ValueError):
                return ""
        try:
            return candidate.resolve().as_uri()
        except (OSError, ValueError):
            return str(candidate)

    def _refresh_locations_map(self) -> None:
        """Push the latest cluster data to the map when it is active."""

        if self._map_view is None:
            return
        if (self._static_selection or "").casefold() != "locations":
            return
        clusters = self._build_location_clusters()
        self._map_view.set_photo_clusters(clusters)
        self._status.showMessage(self._format_location_status(clusters))

    def _handle_cluster_clicked(self, location_name: str) -> None:
        """Filter the gallery to the photos tagged with *location_name*."""

        if not location_name:
            return
        self._active_location_name = location_name
        self._asset_model.set_filter_mode(f"location:{location_name}")
        self._view_controller.show_gallery_view()
        root = self._context.library.root()
        root_display = str(root) if root is not None else "Unbound"
        self._album_label.setText(f"{location_name} — {root_display}")
        self.update_status()

    def _handle_asset_model_reset(self) -> None:
        """Ensure the map reflects the asset model after a full reset."""

        self._refresh_locations_map()

    def _handle_asset_model_rows_changed(
        self, _parent: QModelIndex, _first: int, _last: int
    ) -> None:
        """Update map data when rows are inserted or removed."""

        self._refresh_locations_map()

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------
    def update_status(self) -> None:
        count = self._asset_model.rowCount()
        if count == 0:
            message = "No assets indexed"
        elif count == 1:
            message = "1 asset indexed"
        else:
            message = f"{count} assets indexed"
        self._status.showMessage(message)

    def prompt_for_basic_library(self) -> None:
        if self._context.library.root() is not None:
            return
        self._dialog.prompt_for_basic_library()

    def static_selection(self) -> Optional[str]:
        return self._static_selection

    def clear_static_selection(self) -> None:
        self._static_selection = None
