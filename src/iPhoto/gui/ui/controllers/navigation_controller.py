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

    # ------------------------------------------------------------------
    # Album management
    # ------------------------------------------------------------------
    def open_album(self, path: Path) -> None:
        # Short-circuit redundant open requests that target the album that is
        # already active. These redundant calls usually originate from the
        # filesystem watcher reacting to a manifest save that the application
        # itself initiated. Treating them as no-ops prevents the gallery view
        # from being shown briefly while the detail pane still has focus, which
        # would otherwise feel like a disruptive flicker to the user.
        if (
            self._facade.current_album
            and self._facade.current_album.root == path
            and self._static_selection is None
        ):
            return

        self._static_selection = None
        self._asset_model.set_filter_mode(None)
        # Always present the gallery grid before loading a new album so any
        # lingering detail state from the previous album does not create an
        # empty detail view while the new model is populating.
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
        album = self._facade.open_album(root)
        if album is None:
            self._static_selection = None
            self._asset_model.set_filter_mode(None)
            return
        album.manifest = {**album.manifest, "title": title}

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
