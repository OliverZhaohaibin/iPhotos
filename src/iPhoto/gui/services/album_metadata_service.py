"""Service that centralises manifest writes for album-related actions."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Sequence, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

from ...errors import IPhotoError
from ...models.album import Album

if TYPE_CHECKING:
    from ...library.manager import LibraryManager
    from ..ui.models.asset_list_model import AssetListModel


class AlbumMetadataService(QObject):
    """Persist manifest mutations on behalf of the GUI while shielding the UI."""

    errorRaised = Signal(str)

    def __init__(
        self,
        *,
        asset_list_model: "AssetListModel",
        current_album_getter: Callable[[], Optional[Album]],
        library_manager_getter: Callable[[], Optional["LibraryManager"]],
        refresh_view: Callable[[Path], None],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._asset_list_model = asset_list_model
        self._current_album_getter = current_album_getter
        self._library_manager_getter = library_manager_getter
        self._refresh_view = refresh_view

    # ------------------------------------------------------------------
    # Public API used by :class:`~iPhoto.gui.facade.AppFacade`
    # ------------------------------------------------------------------
    def set_album_cover(self, album: Album, rel: str) -> bool:
        """Set *rel* as cover for *album* and persist the change."""

        if not rel:
            return False
        album.set_cover(rel)
        return self._save_manifest(album)

    def toggle_featured(self, album: Album, ref: str) -> bool:
        """Toggle *ref* inside *album* and mirror updates to the library root."""

        if not ref:
            return False

        featured = album.manifest.setdefault("featured", [])
        was_featured = ref in featured
        desired_state = not was_featured

        library_root: Optional[Path] = None
        root_album: Optional[Album] = None
        root_ref: Optional[str] = None

        manager = self._library_manager_getter()
        if manager is not None:
            library_root = manager.root()

        if library_root is not None and library_root != album.root:
            try:
                absolute_asset = (album.root / ref).resolve()
                root_relative = absolute_asset.relative_to(library_root.resolve())
            except (OSError, ValueError):
                root_ref = None
            else:
                root_ref = root_relative.as_posix()
                try:
                    root_album = Album.open(library_root)
                except IPhotoError as exc:
                    self.errorRaised.emit(str(exc))
                    root_album = None

        if desired_state:
            album.add_featured(ref)
            if root_album is not None and root_ref is not None:
                root_album.add_featured(root_ref)
        else:
            album.remove_featured(ref)
            if root_album is not None and root_ref is not None:
                root_album.remove_featured(root_ref)

        current_saved = self._save_manifest(album, reload_view=False)
        root_saved = True
        if root_album is not None and root_ref is not None:
            root_saved = self._save_manifest(root_album, reload_view=False)

        if current_saved and root_saved:
            self._asset_list_model.update_featured_status(ref, desired_state)
            return desired_state

        # Persistence failed. Roll back to the previous manifest state so the
        # in-memory representation stays consistent with the on-disk version.
        if desired_state:
            album.remove_featured(ref)
            if root_album is not None and root_ref is not None:
                root_album.remove_featured(root_ref)
        else:
            album.add_featured(ref)
            if root_album is not None and root_ref is not None:
                root_album.add_featured(root_ref)
        return was_featured

    def ensure_featured_entries(
        self,
        root: Path,
        imported: Sequence[Path],
    ) -> None:
        """Flag imported assets under *root* as featured when requested."""

        if not imported:
            return

        album = self._resolve_album_for_root(root)
        if album is None:
            return

        updated = False
        for path in imported:
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            album.add_featured(rel)
            updated = True

        if not updated:
            return

        self._save_manifest(album, reload_view=False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _resolve_album_for_root(self, root: Path) -> Optional[Album]:
        """Return an :class:`Album` instance representing *root*."""

        current = self._current_album_getter()
        if current is not None and current.root == root:
            return current
        try:
            return Album.open(root)
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return None

    def _save_manifest(self, album: Album, *, reload_view: bool = True) -> bool:
        """Persist *album* to disk, optionally requesting a UI refresh."""

        manager = self._library_manager_getter()
        if manager is not None:
            manager.pause_watcher()
        try:
            album.save()
        except IPhotoError as exc:
            self.errorRaised.emit(str(exc))
            return False
        finally:
            if manager is not None:
                QTimer.singleShot(250, manager.resume_watcher)

        if reload_view:
            self._refresh_view(album.root)
        return True


__all__ = ["AlbumMetadataService"]
