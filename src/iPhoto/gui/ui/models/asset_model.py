"""Qt model that exposes album assets to views."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, QSize
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap

from ....cache.index_store import IndexStore
from ....config import WORK_DIR_NAME
from ....utils.jsonio import read_json
from ....utils.pathutils import ensure_work_dir
from ...facade import AppFacade


class Roles(IntEnum):
    """Custom roles exposed to QML or widgets."""

    REL = Qt.UserRole + 1
    ABS = Qt.UserRole + 2
    ASSET_ID = Qt.UserRole + 3
    IS_IMAGE = Qt.UserRole + 4
    IS_VIDEO = Qt.UserRole + 5
    IS_LIVE = Qt.UserRole + 6
    LIVE_GROUP_ID = Qt.UserRole + 7
    SIZE = Qt.UserRole + 8
    DT = Qt.UserRole + 9
    FEATURED = Qt.UserRole + 10


class AssetModel(QAbstractListModel):
    """List model combining ``index.jsonl`` and ``links.json`` data."""

    def __init__(self, facade: AppFacade) -> None:
        super().__init__()
        self._facade = facade
        self._album_root: Optional[Path] = None
        self._rows: List[Dict[str, object]] = []
        self._thumb_cache: Dict[str, QPixmap] = {}
        self._thumb_size = QSize(192, 192)
        facade.albumOpened.connect(self._on_album_opened)
        facade.indexUpdated.connect(self._on_index_updated)
        facade.linksUpdated.connect(self._on_links_updated)

    # ------------------------------------------------------------------
    # Qt model implementation
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex | None = None) -> int:  # type: ignore[override]
        if parent is not None and parent.isValid():  # pragma: no cover - tree fallback
            return 0
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # type: ignore[override]
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == Qt.DisplayRole:
            return row["name"]
        if role == Qt.DecorationRole:
            return self._resolve_thumbnail(row)
        if role == Qt.SizeHintRole:
            return QSize(self._thumb_size.width() + 24, self._thumb_size.height() + 48)
        if role == Roles.REL:
            return row["rel"]
        if role == Roles.ABS:
            return row["abs"]
        if role == Roles.ASSET_ID:
            return row["id"]
        if role == Roles.IS_IMAGE:
            return row["is_image"]
        if role == Roles.IS_VIDEO:
            return row["is_video"]
        if role == Roles.IS_LIVE:
            return row["is_live"]
        if role == Roles.LIVE_GROUP_ID:
            return row["live_group_id"]
        if role == Roles.SIZE:
            return row["size"]
        if role == Roles.DT:
            return row["dt"]
        if role == Roles.FEATURED:
            return row["featured"]
        return None

    def roleNames(self) -> Dict[int, bytes]:  # type: ignore[override]
        names = super().roleNames()
        names.update(
            {
                Roles.REL: b"rel",
                Roles.ABS: b"abs",
                Roles.ASSET_ID: b"assetId",
                Roles.IS_IMAGE: b"isImage",
                Roles.IS_VIDEO: b"isVideo",
                Roles.IS_LIVE: b"isLive",
                Roles.LIVE_GROUP_ID: b"liveGroupId",
                Roles.SIZE: b"size",
                Roles.DT: b"dt",
                Roles.FEATURED: b"featured",
            }
        )
        return names

    # ------------------------------------------------------------------
    # Facade callbacks
    # ------------------------------------------------------------------
    def _on_album_opened(self, root: Path) -> None:
        self._album_root = root
        self._reload()

    def _on_index_updated(self, root: Path) -> None:
        if self._album_root and root == self._album_root:
            self._reload()

    def _on_links_updated(self, root: Path) -> None:
        if self._album_root and root == self._album_root:
            self._reload()

    # ------------------------------------------------------------------
    # Data loading helpers
    # ------------------------------------------------------------------
    def _reload(self) -> None:
        if not self._album_root:
            return
        ensure_work_dir(self._album_root, WORK_DIR_NAME)
        manifest = self._facade.current_album.manifest if self._facade.current_album else {}
        featured: set[str] = set(manifest.get("featured", []))
        index_rows = list(IndexStore(self._album_root).read_all())
        live_map = self._load_live_map(self._album_root)

        payload: List[Dict[str, object]] = []
        for row in index_rows:
            rel = str(row["rel"])
            abs_path = str((self._album_root / rel).resolve())
            mime = (row.get("mime") or "").lower()
            is_image = mime.startswith("image/")
            is_video = mime.startswith("video/")
            live_info = live_map.get(rel)
            entry: Dict[str, object] = {
                "rel": rel,
                "abs": abs_path,
                "id": row.get("id", rel),
                "name": Path(rel).name,
                "is_image": is_image,
                "is_video": is_video,
                "is_live": bool(live_info),
                "live_group_id": live_info[0] if live_info else None,
                "size": self._determine_size(row, is_image),
                "dt": row.get("dt"),
                "featured": self._is_featured(rel, featured),
            }
            payload.append(entry)

        self.beginResetModel()
        self._rows = payload
        # Drop any thumbnails that no longer correspond to a listed asset.
        active = {row_data["rel"] for row_data in payload}
        self._thumb_cache = {rel: pix for rel, pix in self._thumb_cache.items() if rel in active}
        self.endResetModel()

    @staticmethod
    def _load_live_map(root: Path) -> Dict[str, tuple[str, str]]:
        path = root / WORK_DIR_NAME / "links.json"
        if not path.exists():
            return {}
        try:
            data = read_json(path)
        except Exception:  # pragma: no cover - invalid JSON handled softly
            return {}
        mapping: Dict[str, tuple[str, str]] = {}
        for group in data.get("live_groups", []):
            gid = group.get("id")
            still = group.get("still")
            motion = group.get("motion")
            if gid and still:
                mapping[str(still)] = (gid, "still")
            if gid and motion:
                mapping[str(motion)] = (gid, "motion")
        return mapping

    @staticmethod
    def _determine_size(row: Dict[str, object], is_image: bool) -> object:
        if is_image:
            return (row.get("w"), row.get("h"))
        return {"bytes": row.get("bytes"), "duration": row.get("dur")}

    @staticmethod
    def _is_featured(rel: str, featured: set[str]) -> bool:
        if rel in featured:
            return True
        live_ref = f"{rel}#live"
        return live_ref in featured

    # ------------------------------------------------------------------
    # Thumbnail helpers
    # ------------------------------------------------------------------
    def _resolve_thumbnail(self, row: Dict[str, object]) -> QPixmap:
        rel = str(row["rel"])
        cached = self._thumb_cache.get(rel)
        if cached is not None:
            return cached
        abs_path = Path(str(row["abs"]))
        is_image = bool(row.get("is_image"))
        is_video = bool(row.get("is_video"))
        pixmap = self._create_thumbnail(abs_path, is_image=is_image, is_video=is_video)
        self._thumb_cache[rel] = pixmap
        return pixmap

    def _create_thumbnail(self, path: Path, *, is_image: bool, is_video: bool) -> QPixmap:
        canvas = QPixmap(self._thumb_size)
        canvas.fill(QColor("#2d2d2d"))
        if is_image:
            source = QPixmap(str(path))
            if not source.isNull():
                scaled = source.scaled(
                    self._thumb_size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                painter = QPainter(canvas)
                painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
                x = (canvas.width() - scaled.width()) // 2
                y = (canvas.height() - scaled.height()) // 2
                painter.drawPixmap(x, y, scaled)
                painter.end()
                return canvas
        # For videos or load failures, draw a placeholder with the file suffix.
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor("#f0f0f0"))
        painter.setBrush(Qt.NoBrush)
        suffix = path.suffix.lower().lstrip(".")
        if is_video and not suffix:
            suffix = "video"
        elif not suffix:
            suffix = "media"
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = suffix.upper()
        text_width = metrics.horizontalAdvance(text)
        text_height = metrics.height()
        painter.drawText(
            (canvas.width() - text_width) // 2,
            (canvas.height() + text_height // 2) // 2,
            text,
        )
        painter.end()
        return canvas
