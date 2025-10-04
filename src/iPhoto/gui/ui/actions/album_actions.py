"""Filesystem-backed album management helpers for the GUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ....config import ALBUM_MANIFEST_NAMES, WORK_DIR_NAME
from ....errors import IPhotoError
from ....models.album import Album
from ....schemas import validate_album
from ....utils.jsonio import write_json


class AlbumActionError(IPhotoError):
    """Raised when album management actions fail."""


@dataclass(slots=True)
class AlbumActions:
    """Encapsulate filesystem mutations for album management.

    The helper keeps the behaviour shared between the sidebar's context menu and
    toolbar actions in one place so that validation and error handling remain
    consistent across the UI.
    """

    manifest_schema: str = "iPhoto/album@1"

    def create_album(self, library_root: Path, title: str) -> Path:
        """Create a new album directory inside *library_root*.

        Parameters
        ----------
        library_root:
            Base directory where album folders live.
        title:
            Desired album title; also used as the directory name.

        Returns
        -------
        Path
            The path to the newly created album directory.
        """

        normalized_title = self._normalise_title(title)
        if not normalized_title:
            raise AlbumActionError("Album name cannot be empty.")
        if "\n" in normalized_title or "\r" in normalized_title:
            raise AlbumActionError("Album name cannot contain newlines.")
        candidate = library_root / normalized_title
        if candidate.exists():
            raise AlbumActionError(f"Album already exists: {candidate.name}")
        candidate.mkdir(parents=True, exist_ok=False)
        manifest_path = candidate / ALBUM_MANIFEST_NAMES[0]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        manifest = {
            "schema": self.manifest_schema,
            "title": normalized_title,
            "created": now,
            "modified": now,
            "cover": "",
            "featured": [],
            "filters": {},
            "tags": [],
        }
        validate_album(manifest)
        write_json(manifest_path, manifest)
        marker = candidate / ".iphoto.album"
        marker.touch(exist_ok=True)
        return candidate

    def rename_album(self, album_path: Path, new_title: str) -> Path:
        """Rename an album directory and update its manifest title."""

        if not album_path.exists():
            raise AlbumActionError(f"Album does not exist: {album_path}")
        normalized_title = self._normalise_title(new_title)
        if not normalized_title:
            raise AlbumActionError("Album name cannot be empty.")
        target = album_path.with_name(normalized_title)
        if target.exists():
            raise AlbumActionError(f"Target already exists: {normalized_title}")
        album = Album.open(album_path)
        album_path.rename(target)
        album.root = target
        album.manifest["title"] = normalized_title
        album.save()
        return target

    def delete_album(self, album_path: Path) -> None:
        """Delete an album directory and all of its contents."""

        if not album_path.exists():
            raise AlbumActionError(f"Album does not exist: {album_path}")
        if not album_path.is_dir():
            raise AlbumActionError(f"Album path is not a directory: {album_path}")
        work_dir = album_path / WORK_DIR_NAME
        if work_dir.exists():
            # Ensure locks are released before deletion to avoid orphaned files.
            for lock in (work_dir / "locks").glob("*.lock"):
                lock.unlink(missing_ok=True)
        for child in sorted(album_path.iterdir(), reverse=True):
            if child.is_dir():
                self._remove_tree(child)
            else:
                child.unlink(missing_ok=True)
        album_path.rmdir()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _normalise_title(self, title: str) -> str:
        sanitized = title.strip()
        if sanitized in {".", ".."}:
            return ""
        return sanitized

    def _remove_tree(self, root: Path) -> None:
        for child in root.iterdir():
            if child.is_dir():
                self._remove_tree(child)
            else:
                child.unlink(missing_ok=True)
        root.rmdir()
