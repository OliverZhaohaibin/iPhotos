"""Album manifest handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..cache.lock import FileLock
from ..config import ALBUM_MANIFEST_NAMES, WORK_DIR_NAME
from ..errors import AlbumNotFoundError
from ..schemas import validate_album
from ..utils.jsonio import read_json, write_json
from ..utils.pathutils import ensure_work_dir


@dataclass(slots=True)
class Album:
    """Represents an album loaded from disk."""

    root: Path
    manifest: Dict[str, Any]

    @staticmethod
    def open(root: Path) -> "Album":
        if not root.exists():
            raise AlbumNotFoundError(f"Album directory does not exist: {root}")
        ensure_work_dir(root, WORK_DIR_NAME)
        manifest_path = Album._find_manifest(root)
        if manifest_path:
            manifest = read_json(manifest_path)
        else:
            manifest = {
                "schema": "iPhoto/album@1",
                "title": root.name,
                "filters": {},
            }
        validate_album(manifest)
        return Album(root, manifest)

    @staticmethod
    def _find_manifest(root: Path) -> Optional[Path]:
        for name in ALBUM_MANIFEST_NAMES:
            candidate = root / name
            if candidate.exists():
                return candidate
        return None

    def save(self) -> Path:
        """Persist the manifest to disk and return the written path."""

        path = self._find_manifest(self.root) or (self.root / ALBUM_MANIFEST_NAMES[0])
        work_dir = self.root / WORK_DIR_NAME / "manifest.bak"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.manifest.setdefault("created", now)
        self.manifest["modified"] = now
        validate_album(self.manifest)
        with FileLock(self.root, "manifest"):
            write_json(path, self.manifest, backup_dir=work_dir)
        return path

    # High-level helpers -------------------------------------------------

    def set_cover(self, rel: str) -> None:
        self.manifest["cover"] = rel

    def add_featured(self, ref: str) -> None:
        featured = self.manifest.setdefault("featured", [])
        if ref not in featured:
            featured.append(ref)

    def remove_featured(self, ref: str) -> None:
        featured = self.manifest.setdefault("featured", [])
        self.manifest["featured"] = [item for item in featured if item != ref]
