"""Controller responsible for drag-and-drop flows in the gallery."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

from PySide6.QtCore import QObject

from ....media_classifier import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
try:  # pragma: no cover - keep compatibility with script execution
    from ...appctx import AppContext
except ImportError:  # pragma: no cover - fallback for ``python -m`` usage
    from iPhotos.src.iPhoto.appctx import AppContext
from ...facade import AppFacade
from ..widgets.asset_grid import AssetGrid
from ..widgets.album_sidebar import AlbumSidebar
from .dialog_controller import DialogController
from .navigation_controller import NavigationController
from .status_bar_controller import StatusBarController


class DragDropController(QObject):
    """Centralise grid and sidebar drop behaviour."""

    def __init__(
        self,
        *,
        grid_view: AssetGrid,
        sidebar: AlbumSidebar,
        context: AppContext,
        facade: AppFacade,
        status_bar: StatusBarController,
        dialog: DialogController,
        navigation: NavigationController,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._grid_view = grid_view
        self._sidebar = sidebar
        self._context = context
        self._facade = facade
        self._status_bar = status_bar
        self._dialog = dialog
        self._navigation = navigation

        self._grid_view.configure_external_drop(
            handler=self._handle_grid_drop,
            validator=self._validate_grid_drop,
        )
        self._sidebar.filesDropped.connect(self._handle_sidebar_drop)

    # ------------------------------------------------------------------
    # Grid drop handlers
    # ------------------------------------------------------------------
    def _validate_grid_drop(self, paths: List[Path]) -> bool:
        images, videos, _ = self._classify_media_paths(paths)
        if not images and not videos:
            return False

        selection = (self._navigation.static_selection() or "").casefold()
        if selection == "live photos":
            return False
        if selection == "videos":
            return bool(videos)
        return bool(images or videos)

    def _handle_grid_drop(self, paths: List[Path]) -> None:
        images, videos, _ = self._classify_media_paths(paths)
        selection = (self._navigation.static_selection() or "").casefold()
        mark_featured = False

        if selection == "videos":
            allowed = videos
        else:
            allowed = images + videos
            mark_featured = selection == "favorites"

        if not allowed:
            self._status_bar.show_message("No supported media files were dropped.", 5000)
            return

        target: Optional[Path]
        if selection:
            target = self._context.library.root()
            if target is None:
                self._dialog.bind_library_dialog()
                return
        else:
            album = self._facade.current_album
            if album is None:
                self._status_bar.show_message(
                    "Open an album before importing files.",
                    5000,
                )
                return
            target = album.root

        self._facade.import_files(
            allowed,
            destination=target,
            mark_featured=mark_featured,
        )

    # ------------------------------------------------------------------
    # Sidebar drop handlers
    # ------------------------------------------------------------------
    def _handle_sidebar_drop(self, target: Path, payload: list[Path]) -> None:
        paths = self._coerce_path_list(payload)
        if not paths:
            return
        images, videos, unsupported = self._classify_media_paths(paths)
        if unsupported:
            self._status_bar.show_message(
                "Only photo and video files can be imported.",
                5000,
            )
            return
        allowed = images + videos
        if not allowed:
            self._status_bar.show_message("No supported media files were dropped.", 5000)
            return
        self._facade.import_files(allowed, destination=target)

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------
    def _classify_media_paths(
        self, paths: Iterable[Path]
    ) -> tuple[List[Path], List[Path], List[Path]]:
        normalized = self._normalize_drop_paths(paths)
        images: List[Path] = []
        videos: List[Path] = []
        unsupported: List[Path] = []
        for path in normalized:
            suffix = path.suffix.lower()
            if not path.exists() or not path.is_file():
                unsupported.append(path)
                continue
            if suffix in IMAGE_EXTENSIONS:
                images.append(path)
            elif suffix in VIDEO_EXTENSIONS:
                videos.append(path)
            else:
                unsupported.append(path)
        return images, videos, unsupported

    def _normalize_drop_paths(self, paths: Iterable[Path]) -> List[Path]:
        normalized: List[Path] = []
        seen: set[Path] = set()
        for path in paths:
            try:
                candidate = Path(path).expanduser()
            except TypeError:
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved in seen:
                continue
            seen.add(resolved)
            normalized.append(resolved)
        return normalized

    def _coerce_path_list(self, payload: object) -> List[Path]:
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, tuple):
            items = list(payload)
        else:
            items = [payload]
        paths: List[Path] = []
        for item in items:
            try:
                paths.append(Path(item))
            except TypeError:
                continue
        return paths
