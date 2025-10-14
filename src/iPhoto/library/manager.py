"""Basic library management: scanning, watching and editing albums."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from ..config import ALBUM_MANIFEST_NAMES, WORK_DIR_NAME
from ..errors import (
    AlbumDepthError,
    AlbumNameConflictError,
    AlbumOperationError,
    LibraryUnavailableError,
)
from ..models.album import Album
from ..utils.jsonio import read_json
from ..cache.index_store import IndexStore
from .tree import AlbumNode


@dataclass(slots=True, frozen=True)
class GeotaggedAsset:
    """Lightweight descriptor describing an asset with GPS metadata."""

    library_relative: str
    """Relative path from the library root to the asset."""

    album_relative: str
    """Relative path from the asset's album root to the file."""

    absolute_path: Path
    """Absolute filesystem path to the asset."""

    album_path: Path
    """Root directory of the album that owns the asset."""

    asset_id: str
    """Identifier reported by the index row."""

    latitude: float
    longitude: float
    is_image: bool
    is_video: bool
    still_image_time: Optional[float]
    duration: Optional[float]


class LibraryManager(QObject):
    """Manage the Basic Library tree and provide file-system helpers."""

    treeUpdated = Signal()
    errorRaised = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._root: Path | None = None
        self._albums: list[AlbumNode] = []
        self._children: Dict[Path, list[AlbumNode]] = {}
        self._nodes: Dict[Path, AlbumNode] = {}
        self._watcher = QFileSystemWatcher(self)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(500)
        # ``_watch_suspend_depth`` tracks how many in-flight operations asked us to
        # ignore file-system notifications.  Using a counter instead of a boolean
        # keeps the logic safe when nested saves occur (for example when both the
        # album manifest and a library-level manifest are updated as part of a
        # single user action).
        self._watch_suspend_depth = 0
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._debounce.timeout.connect(self._refresh_tree)

    # ------------------------------------------------------------------
    # Basic properties
    # ------------------------------------------------------------------
    def root(self) -> Path | None:
        return self._root

    # ------------------------------------------------------------------
    # Binding and scanning
    # ------------------------------------------------------------------
    def bind_path(self, root: Path) -> None:
        normalized = root.expanduser().resolve()
        if not normalized.exists() or not normalized.is_dir():
            raise LibraryUnavailableError(f"Library path does not exist: {root}")
        self._root = normalized
        self._refresh_tree()

    def list_albums(self) -> list[AlbumNode]:
        return list(self._albums)

    def list_children(self, album: AlbumNode) -> list[AlbumNode]:
        return list(self._children.get(album.path, []))

    def scan_tree(self) -> list[AlbumNode]:
        self._refresh_tree()
        return self.list_albums()

    # ------------------------------------------------------------------
    # Asset helpers
    # ------------------------------------------------------------------
    def get_geotagged_assets(self) -> List[GeotaggedAsset]:
        """Return every asset in the library that exposes GPS coordinates."""

        root = self._require_root()
        # ``seen`` prevents duplicate entries when a sub-album and its parent
        # both reference the same physical file in their indexes.
        seen: set[Path] = set()
        assets: list[GeotaggedAsset] = []

        album_paths: set[Path] = {root}
        album_paths.update(self._nodes.keys())

        for album_path in sorted(album_paths):
            try:
                rows = IndexStore(album_path).read_all()
            except Exception:
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                gps = row.get("gps")
                if not isinstance(gps, dict):
                    continue
                lat = gps.get("lat")
                lon = gps.get("lon")
                if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                    continue
                rel = row.get("rel")
                if not isinstance(rel, str) or not rel:
                    continue
                abs_path = (album_path / rel).resolve()
                if abs_path in seen:
                    continue
                seen.add(abs_path)
                try:
                    library_relative_path = abs_path.relative_to(root)
                    library_relative_str = library_relative_path.as_posix()
                except ValueError:
                    library_relative_str = abs_path.name
                asset_id = str(row.get("id") or rel)
                is_image = bool(row.get("is_image"))
                is_video = bool(row.get("is_video"))
                still_image_time = row.get("still_image_time")
                if isinstance(still_image_time, (int, float)):
                    still_image_value: Optional[float] = float(still_image_time)
                else:
                    still_image_value = None
                duration = row.get("dur")
                if isinstance(duration, (int, float)):
                    duration_value: Optional[float] = float(duration)
                else:
                    duration_value = None
                assets.append(
                    GeotaggedAsset(
                        library_relative=library_relative_str,
                        album_relative=rel,
                        absolute_path=abs_path,
                        album_path=album_path,
                        asset_id=asset_id,
                        latitude=float(lat),
                        longitude=float(lon),
                        is_image=is_image,
                        is_video=is_video,
                        still_image_time=still_image_value,
                        duration=duration_value,
                    )
                )

        assets.sort(key=lambda item: item.library_relative)
        return assets

    # ------------------------------------------------------------------
    # Album creation helpers
    # ------------------------------------------------------------------
    def create_album(self, name: str) -> AlbumNode:
        root = self._require_root()
        target = self._validate_new_name(root, name)
        target.mkdir(parents=False, exist_ok=False)
        node = AlbumNode(target, 1, target.name, False)
        self.ensure_manifest(node)
        self._refresh_tree()
        return self._node_for_path(target)

    def create_subalbum(self, parent: AlbumNode, name: str) -> AlbumNode:
        if parent.level != 1:
            raise AlbumDepthError("Sub-albums can only be created under top-level albums.")
        root = self._require_root()
        if not parent.path.is_relative_to(root):
            parent_path = parent.path.resolve()
            if not str(parent_path).startswith(str(root)):
                raise AlbumOperationError("Parent album is outside the library root.")
        target = self._validate_new_name(parent.path, name)
        target.mkdir(parents=False, exist_ok=False)
        node = AlbumNode(target, 2, target.name, False)
        self.ensure_manifest(node)
        self._refresh_tree()
        return self._node_for_path(target)

    def rename_album(self, node: AlbumNode, new_name: str) -> None:
        parent = node.path.parent
        target = self._validate_new_name(parent, new_name)
        try:
            node.path.rename(target)
        except FileExistsError as exc:
            raise AlbumNameConflictError(f"An album named '{new_name}' already exists.") from exc
        except OSError as exc:  # pragma: no cover - defensive guard
            raise AlbumOperationError(str(exc)) from exc
        album = Album.open(target)
        album.manifest["title"] = new_name
        album.save()
        self._refresh_tree()

    def ensure_manifest(self, node: AlbumNode) -> Path:
        manifest = self._find_manifest(node.path)
        if manifest:
            return manifest
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        album = Album(node.path, {
            "schema": "iPhoto/album@1",
            "title": node.title,
            "created": now,
            "modified": now,
            "cover": "",
            "featured": [],
            "filters": {},
            "tags": [],
        })
        album.save()
        marker = node.path / ".iphoto.album"
        if not marker.exists():
            marker.touch()
        return self._find_manifest(node.path) or (node.path / ALBUM_MANIFEST_NAMES[0])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _require_root(self) -> Path:
        if self._root is None:
            raise LibraryUnavailableError("Basic Library path has not been configured.")
        return self._root

    def _refresh_tree(self) -> None:
        if self._root is None:
            self._albums = []
            self._children = {}
            self._nodes = {}
            self._rebuild_watches()
            self.treeUpdated.emit()
            return
        albums: list[AlbumNode] = []
        children: Dict[Path, list[AlbumNode]] = {}
        nodes: Dict[Path, AlbumNode] = {}
        for album_dir in self._iter_album_dirs(self._root):
            node = self._build_node(album_dir, level=1)
            albums.append(node)
            nodes[album_dir] = node
            child_nodes = [self._build_node(child, level=2) for child in self._iter_album_dirs(album_dir)]
            for child in child_nodes:
                nodes[child.path] = child
            children[album_dir] = child_nodes
        self._albums = sorted(albums, key=lambda item: item.title.casefold())
        self._children = {parent: sorted(kids, key=lambda item: item.title.casefold()) for parent, kids in children.items()}
        self._nodes = nodes
        self._rebuild_watches()
        self.treeUpdated.emit()

    def _iter_album_dirs(self, root: Path) -> Iterable[Path]:
        try:
            entries = list(root.iterdir())
        except OSError as exc:  # pragma: no cover - filesystem failure
            self.errorRaised.emit(str(exc))
            return []
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name == WORK_DIR_NAME:
                continue
            yield entry

    def _build_node(self, path: Path, *, level: int) -> AlbumNode:
        title, has_manifest = self._describe_album(path)
        return AlbumNode(path, level, title, has_manifest)

    def _describe_album(self, path: Path) -> tuple[str, bool]:
        manifest = self._find_manifest(path)
        if manifest:
            try:
                data = read_json(manifest)
            except Exception as exc:  # pragma: no cover - invalid JSON
                self.errorRaised.emit(str(exc))
            else:
                title = str(data.get("title") or path.name)
                return title, True
            return path.name, True
        marker = path / ".iphoto.album"
        if marker.exists():
            return path.name, True
        return path.name, False

    def _find_manifest(self, path: Path) -> Path | None:
        for name in ALBUM_MANIFEST_NAMES:
            candidate = path / name
            if candidate.exists():
                return candidate
        return None

    def _validate_new_name(self, parent: Path, name: str) -> Path:
        candidate = name.strip()
        if not candidate:
            raise AlbumOperationError("Album name cannot be empty.")
        if Path(candidate).name != candidate:
            raise AlbumOperationError("Album name must not contain path separators.")
        target = parent / candidate
        if target.exists():
            raise AlbumNameConflictError(f"An album named '{candidate}' already exists.")
        return target

    def pause_watcher(self) -> None:
        """Temporarily suppress change notifications during internal writes."""

        # Increment the suspension depth so nested pause calls continue to be
        # reference-counted.  The debounce timer is stopped on the first pause
        # to ensure that an earlier notification does not race with the write we
        # are about to perform.
        self._watch_suspend_depth += 1
        if self._watch_suspend_depth == 1 and self._debounce.isActive():
            self._debounce.stop()

    def resume_watcher(self) -> None:
        """Re-enable change notifications once protected writes have finished."""

        if self._watch_suspend_depth == 0:
            return
        self._watch_suspend_depth -= 1

    def _on_directory_changed(self, path: str) -> None:
        # Skip notifications while we are in the middle of an internally
        # triggered write such as a manifest save.  The associated UI components
        # already know about those updates, so reacting to the file-system event
        # would only cause redundant reloads.
        if self._watch_suspend_depth > 0:
            return

        # ``QFileSystemWatcher`` emits plain strings.  Queue a debounced refresh
        # whenever a change notification arrives so the sidebar reflects
        # external edits without thrashing the filesystem.
        self._debounce.start()

    def _rebuild_watches(self) -> None:
        current = set(self._watcher.directories())
        desired: set[str] = set()
        if self._root is not None:
            desired.add(str(self._root))
            desired.update(str(node.path) for node in self._albums)
        remove = [path for path in current if path not in desired]
        if remove:
            self._watcher.removePaths(remove)
        add = [path for path in desired if path not in current]
        if add:
            self._watcher.addPaths(add)

    def _node_for_path(self, path: Path) -> AlbumNode:
        node = self._nodes.get(path)
        if node is not None:
            return node
        resolved = path.resolve()
        node = self._nodes.get(resolved)
        if node is not None:
            return node
        raise AlbumOperationError(f"Album node not found for path: {path}")

__all__ = ["LibraryManager"]
