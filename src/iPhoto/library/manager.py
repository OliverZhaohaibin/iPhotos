"""Basic library management: scanning, watching and editing albums."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import time_ns
from typing import Dict, Iterable

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
from .tree import AlbumNode


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
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._debounce.timeout.connect(self._refresh_tree)
        # ``_immunity_tokens`` remembers manifest paths the application has
        # written moments ago.  When the filesystem watcher later reports a
        # change under the same directory, we can compare timestamps and ignore
        # notifications that clearly originate from our own saves.
        self._immunity_tokens: Dict[Path, int] = {}

    # ``_WRITE_IMMUNITY_NS`` defines how long a manifest write is considered
    # trusted.  Two seconds is comfortably longer than the debounce timer while
    # still short enough that external edits quickly become visible again.
    _WRITE_IMMUNITY_NS = 2_000_000_000

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

    def register_internal_write(self, path: Path, token: str) -> None:
        """Record an internal manifest write so watcher callbacks can ignore it."""

        try:
            timestamp = int(token)
        except (TypeError, ValueError):
            # Fall back to the current time when the token cannot be parsed.  A
            # best-effort value is sufficient because the signal only ever
            # originates from trusted application code.
            timestamp = time_ns()
        resolved = path.resolve()
        self._immunity_tokens[resolved] = timestamp
        self._prune_immunity_tokens(time_ns())

    def _on_directory_changed(self, path_str: str) -> None:
        """React to watcher updates while honouring internal-write immunity."""

        changed_dir = Path(path_str).resolve()
        now = time_ns()
        self._prune_immunity_tokens(now)

        # Iterate over a snapshot of the immunity table so we can freely mutate
        # it during the loop without invalidating the iterator.  Storing
        # ``manifest_path`` as a resolved absolute path ensures we are comparing
        # like-for-like values here even when the watcher delivers symlinked or
        # differently-cased directory strings on macOS/Windows.
        for manifest_path, timestamp in list(self._immunity_tokens.items()):
            # Some platforms notify parent or ancestor directories rather than
            # the folder that actually contains the manifest.  Accept the
            # change when the reported directory lies anywhere along the
            # manifest's parent chain so our immunity logic stays robust across
            # varying watcher behaviours.
            if changed_dir in manifest_path.parents:
                if now - timestamp <= self._WRITE_IMMUNITY_NS:
                    # The manifest that triggered this notification was written
                    # by the application itself just moments ago.  Drop the
                    # token so a subsequent genuine external edit will still be
                    # detected and exit early to prevent an unnecessary tree
                    # refresh.
                    del self._immunity_tokens[manifest_path]
                    return
                # The manifest lives under the reported directory but the
                # recorded timestamp has fallen outside the immunity window,
                # meaning the change we are observing is not linked to the
                # known internal operation.  Remove the stale token so future
                # internal writes can register fresh entries.
                del self._immunity_tokens[manifest_path]
                continue
            # Keep immunity tokens whose directories do not match the watcher
            # notification so they can still protect subsequent callbacks for
            # the manifests that have not yet reported in.

        self._debounce.start()

    def _rebuild_watches(self) -> None:
        current = set(self._watcher.directories())
        desired: set[str] = set()
        if self._root is not None:
            desired.add(str(self._root))
            # ``_nodes`` contains every discovered album node, including nested
            # sub-albums.  Watching each directory individually ensures the
            # filesystem notifications we receive match the exact manifest
            # location, allowing the immunity token logic to recognise internal
            # writes reliably even for deeply nested structures.
            desired.update(str(path) for path in self._nodes)
        remove = [path for path in current if path not in desired]
        if remove:
            self._watcher.removePaths(remove)
        add = [path for path in desired if path not in current]
        if add:
            self._watcher.addPaths(add)

    def _prune_immunity_tokens(self, now: int) -> None:
        """Discard outdated immunity tokens to keep the map small."""

        expired = [
            manifest for manifest, timestamp in self._immunity_tokens.items()
            if now - timestamp > self._WRITE_IMMUNITY_NS
        ]
        for manifest in expired:
            self._immunity_tokens.pop(manifest, None)

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
