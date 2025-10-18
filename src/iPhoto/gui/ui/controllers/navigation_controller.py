"""Coordinate album navigation and sidebar selections."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QLabel, QStatusBar

# Support both package-style and legacy ``iPhotos.src`` imports during GUI
# bootstrap.
try:  # pragma: no cover - path-sensitive import
    from ...appctx import AppContext
except ImportError:  # pragma: no cover - executed in script mode
    from iPhotos.src.iPhoto.appctx import AppContext
from ...facade import AppFacade
from ..models.asset_model import AssetModel
from ..widgets.album_sidebar import AlbumSidebar
from .dialog_controller import DialogController
from .view_controller import ViewController


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
    ) -> None:
        self._context = context
        self._facade = facade
        self._asset_model = asset_model
        self._sidebar = sidebar
        self._album_label = album_label
        self._status = status_bar
        self._dialog = dialog
        self._view_controller = view_controller
        self._static_selection: Optional[str] = None
        # ``_last_open_was_refresh`` records whether ``open_album`` most recently
        # reissued the currently open album.  When ``True`` the main window can
        # keep the detail pane visible rather than reverting to the gallery.
        self._last_open_was_refresh: bool = False

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
        self._asset_model.set_filter_mode(None)
        # Returning to a real album should always restore the traditional grid
        # presentation before the model finishes loading.
        self._view_controller.restore_default_gallery()
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
        self._view_controller.restore_default_gallery()
        self.open_static_collection(AlbumSidebar.ALL_PHOTOS_TITLE, None)

    def open_static_node(self, title: str) -> None:
        mapping = {
            "videos": "videos",
            "live photos": "live",
            "favorites": "favorites",
        }
        key = title.casefold()
        mode = mapping.get(key, None)
        self.open_static_collection(title, mode)

    def open_location_view(self) -> None:
        """Activate the Location view without forcing the gallery grid."""

        self.open_static_collection("Location", None, show_gallery=False)

    def open_static_collection(
        self,
        title: str,
        filter_mode: Optional[str],
        *,
        show_gallery: bool = True,
    ) -> None:
        root = self._context.library.root()
        if root is None:
            self._dialog.bind_library_dialog()
            return
        # Reset the detail pane whenever a static collection (All Photos,
        # Favorites, etc.) is opened so the UI consistently shows the grid as
        # its entry point for that virtual album.
        if show_gallery:
            self._view_controller.restore_default_gallery()
            self._view_controller.show_gallery_view()
        self._asset_model.set_filter_mode(filter_mode)
        self._static_selection = title
        album = self._facade.open_album(root)
        if album is None:
            self._static_selection = None
            self._asset_model.set_filter_mode(None)
            return
        album.manifest = {**album.manifest, "title": title}

    def consume_last_open_refresh(self) -> bool:
        """Return ``True`` if the previous :meth:`open_album` was a refresh."""

        was_refresh = self._last_open_was_refresh
        self._last_open_was_refresh = False
        return was_refresh

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

    def is_all_photos_view(self) -> bool:
        """Return ``True`` when the "All Photos" virtual collection is active."""

        # ``_static_selection`` mirrors the last sidebar node that activated a
        # static collection.  Compare it against the well-known label using a
        # case-insensitive check so localisation or theme adjustments that tweak
        # the capitalisation do not affect the outcome.
        if not self._static_selection:
            return False
        return (
            self._static_selection.casefold()
            == AlbumSidebar.ALL_PHOTOS_TITLE.casefold()
        )
