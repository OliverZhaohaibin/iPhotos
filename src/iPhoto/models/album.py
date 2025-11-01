"""Album manifest handling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..cache.lock import FileLock
from ..config import ALBUM_MANIFEST_NAMES, WORK_DIR_NAME
from ..errors import AlbumNotFoundError, IPhotoError
from ..schemas import validate_album
from ..utils.jsonio import read_json, write_json
from ..utils.logging import logger
from ..utils.pathutils import ensure_work_dir


@dataclass(slots=True)
class Album:
    """Represents an album loaded from disk."""

    root: Path
    manifest: Dict[str, Any]

    @classmethod
    def open(cls, root: Path) -> "Album":
        if not root.exists():
            raise AlbumNotFoundError(f"Album directory does not exist: {root}")

        ensure_work_dir(root, WORK_DIR_NAME)

        manifest_path = cls._find_manifest(root)
        changed = False

        def _default_manifest() -> dict[str, Any]:
            """Build a minimal but fully valid manifest for *root*."""

            return {
                "schema": "iPhoto/album@1",
                "id": str(uuid.uuid4()),
                "title": root.name,
                "filters": {},
            }

        if manifest_path is None:
            manifest_path = root / ALBUM_MANIFEST_NAMES[0]
            manifest: dict[str, Any] = _default_manifest()
            changed = True
        else:
            try:
                manifest = read_json(manifest_path)
            except IPhotoError as exc:
                logger.error("Failed to read manifest %s: %s", manifest_path, exc)
                manifest = _default_manifest()
                changed = True

        # Normalise required fields so every caller sees a fully populated
        # manifest.  This is critical when move operations capture album IDs
        # immediately after a folder is created.
        if manifest.get("schema") != "iPhoto/album@1":
            manifest["schema"] = "iPhoto/album@1"
            changed = True

        title = manifest.get("title")
        if not isinstance(title, str) or not title:
            manifest["title"] = root.name
            changed = True
        elif title != root.name:
            manifest["title"] = root.name
            changed = True

        if not manifest.get("id"):
            manifest["id"] = str(uuid.uuid4())
            changed = True

        filters = manifest.get("filters")
        if filters is None:
            manifest["filters"] = {}
            changed = True
        elif not isinstance(filters, dict):
            manifest["filters"] = {}
            changed = True

        try:
            validate_album(manifest)
        except IPhotoError as exc:
            logger.error(
                "Manifest for %s failed validation (%s); regenerating with defaults.",
                root,
                exc,
            )
            manifest = _default_manifest()
            changed = True
            validate_album(manifest)

        if changed:
            backup_dir = root / WORK_DIR_NAME / "manifest.bak"
            try:
                with FileLock(root, "manifest"):
                    write_json(manifest_path, manifest, backup_dir=backup_dir)
            except IPhotoError as exc:
                logger.warning(
                    "Failed to persist manifest updates for %s: %s", root, exc
                )

        return cls(root, manifest)

    @staticmethod
    def _find_manifest(root: Path) -> Optional[Path]:
        for name in ALBUM_MANIFEST_NAMES:
            candidate = root / name
            if candidate.exists():
                return candidate
        return None

    def save(self) -> Path:
        """Persist the manifest to disk."""

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
