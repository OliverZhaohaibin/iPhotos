"""List model combining ``index.jsonl`` and ``links.json`` data."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap

from ....cache.index_store import IndexStore
from ....config import WORK_DIR_NAME
from ....utils.pathutils import ensure_work_dir
from ...facade import AppFacade
from ..tasks.thumbnail_loader import ThumbnailLoader
from .live_map import load_live_map
from .roles import Roles, role_names

_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".heifs",
    ".heicf",
}

_VIDEO_EXTENSIONS = {
    ".mov",
    ".mp4",
    ".m4v",
    ".qt",
}


class AssetListModel(QAbstractListModel):
    """Expose album assets to Qt views."""

    def __init__(self, facade: AppFacade, parent=None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self._facade = facade
        self._album_root: Optional[Path] = None
        self._rows: List[Dict[str, object]] = []
        self._row_lookup: Dict[str, int] = {}
        self._thumb_cache: Dict[str, QPixmap] = {}
        self._placeholder_cache: Dict[str, QPixmap] = {}
        self._thumb_size = QSize(192, 192)
        self._thumb_loader = ThumbnailLoader(self)
        self._thumb_loader.ready.connect(self._on_thumb_ready)
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
            return ""
        if role == Qt.DecorationRole:
            return self._resolve_thumbnail(row)
        if role == Qt.SizeHintRole:
            return QSize(self._thumb_size.width(), self._thumb_size.height())
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
        if role == Roles.LIVE_MOTION_REL:
            return row["live_motion"]
        if role == Roles.SIZE:
            return row["size"]
        if role == Roles.DT:
            return row["dt"]
        if role == Roles.FEATURED:
            return row["featured"]
        return None

    def roleNames(self) -> Dict[int, bytes]:  # type: ignore[override]
        return role_names(super().roleNames())

    def thumbnail_loader(self) -> ThumbnailLoader:
        return self._thumb_loader

    # ------------------------------------------------------------------
    # Facade callbacks
    # ------------------------------------------------------------------
    def _on_album_opened(self, root: Path) -> None:
        self._album_root = root
        self._thumb_loader.reset_for_album(root)
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
        live_map = load_live_map(self._album_root)

        payload: List[Dict[str, object]] = []
        for row in index_rows:
            rel = str(row["rel"])
            live_info = live_map.get(rel)
            if live_info and live_info.get("role") == "motion" and live_info.get("still"):
                continue

            abs_path = str((self._album_root / rel).resolve())
            is_image, is_video = self._classify_media(row)
            live_motion: Optional[str] = None
            live_group_id: Optional[str] = None
            if live_info and live_info.get("role") == "still":
                motion_rel = live_info.get("motion")
                if isinstance(motion_rel, str) and motion_rel:
                    live_motion = motion_rel
                group_id = live_info.get("id")
                if isinstance(group_id, str):
                    live_group_id = group_id
            elif live_info and isinstance(live_info.get("id"), str):
                live_group_id = live_info["id"]  # pragma: no cover - motion branch skipped

            entry: Dict[str, object] = {
                "rel": rel,
                "abs": abs_path,
                "id": row.get("id", rel),
                "name": Path(rel).name,
                "is_image": is_image,
                "is_video": is_video,
                "is_live": bool(live_motion),
                "live_group_id": live_group_id,
                "live_motion": live_motion,
                "size": self._determine_size(row, is_image),
                "dt": row.get("dt"),
                "featured": self._is_featured(rel, featured),
                "still_image_time": row.get("still_image_time"),
                "dur": row.get("dur"),
            }
            payload.append(entry)

        self.beginResetModel()
        self._rows = payload
        self._row_lookup = {row_data["rel"]: idx for idx, row_data in enumerate(payload)}
        active = set(self._row_lookup.keys())
        self._thumb_cache = {rel: pix for rel, pix in self._thumb_cache.items() if rel in active}
        self.endResetModel()

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

    @staticmethod
    def _classify_media(row: Dict[str, object]) -> Tuple[bool, bool]:
        mime_raw = row.get("mime")
        mime = mime_raw.lower() if isinstance(mime_raw, str) else ""
        is_image = mime.startswith("image/")
        is_video = mime.startswith("video/")
        if is_image or is_video:
            return is_image, is_video

        legacy_kind = row.get("type")
        if isinstance(legacy_kind, str):
            kind = legacy_kind.lower()
            if kind == "image":
                return True, False
            if kind == "video":
                return False, True

        suffix = Path(str(row.get("rel", ""))).suffix.lower()
        if suffix in _IMAGE_EXTENSIONS:
            return True, False
        if suffix in _VIDEO_EXTENSIONS:
            return False, True
        return False, False

    # ------------------------------------------------------------------
    # Thumbnail helpers
    # ------------------------------------------------------------------
    def _resolve_thumbnail(self, row: Dict[str, object]) -> QPixmap:
        rel = str(row["rel"])
        cached = self._thumb_cache.get(rel)
        if cached is not None:
            return cached
        placeholder = self._placeholder_for(rel, bool(row.get("is_video")))
        if not self._album_root:
            return placeholder
        abs_path = Path(str(row["abs"]))
        if bool(row.get("is_image")):
            pixmap = self._thumb_loader.request(rel, abs_path, self._thumb_size, is_image=True)
            if pixmap is not None:
                self._thumb_cache[rel] = pixmap
                return pixmap
        if bool(row.get("is_video")):
            still_time = row.get("still_image_time")
            duration = row.get("dur")
            still_hint: Optional[float] = float(still_time) if isinstance(still_time, (int, float)) else None
            duration_value: Optional[float] = float(duration) if isinstance(duration, (int, float)) else None
            if still_hint is not None and duration_value and duration_value > 0:
                max_seek = max(duration_value - 0.01, 0.0)
                if still_hint > max_seek:
                    still_hint = max_seek
            pixmap = self._thumb_loader.request(
                rel,
                abs_path,
                self._thumb_size,
                is_image=False,
                is_video=True,
                still_image_time=still_hint,
                duration=duration_value,
            )
            if pixmap is not None:
                self._thumb_cache[rel] = pixmap
                return pixmap
        return placeholder

    def _on_thumb_ready(self, root: Path, rel: str, pixmap: QPixmap) -> None:
        if not self._album_root or root != self._album_root:
            return
        self._thumb_cache[rel] = pixmap
        index = self._row_lookup.get(rel)
        if index is None:
            return
        model_index = self.index(index, 0)
        self.dataChanged.emit(model_index, model_index, [Qt.DecorationRole])

    def _placeholder_for(self, rel: str, is_video: bool) -> QPixmap:
        suffix = Path(rel).suffix.lower().lstrip(".")
        if not suffix:
            suffix = "video" if is_video else "media"
        key = f"{suffix}|{is_video}"
        cached = self._placeholder_cache.get(key)
        if cached is not None:
            return cached
        canvas = QPixmap(self._thumb_size)
        canvas.fill(QColor("#1b1b1b"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor("#f0f0f0"))
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        label = suffix.upper()
        text_width = metrics.horizontalAdvance(label)
        baseline = (canvas.height() + metrics.ascent()) // 2
        painter.drawText((canvas.width() - text_width) // 2, baseline, label)
        painter.end()
        self._placeholder_cache[key] = canvas
        return canvas
