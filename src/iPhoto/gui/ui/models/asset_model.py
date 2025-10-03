"""Qt model that exposes album assets to views."""

from __future__ import annotations

import hashlib
from enum import IntEnum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QRunnable,
    QThreadPool,
    Qt,
    QSize,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QImageReader, QPainter, QPixmap
from PIL import Image, ImageOps
from PIL.ImageQt import ImageQt
import pillow_heif

pillow_heif.register_heif_opener()

from ....cache.index_store import IndexStore
from ....config import WORK_DIR_NAME
from ....utils.jsonio import read_json
from ....utils.pathutils import ensure_work_dir
from ...facade import AppFacade


class _ThumbnailJob(QRunnable):
    """Background task that renders a thumbnail ``QImage``."""

    def __init__(
        self,
        loader: "_ThumbnailLoader",
        rel: str,
        abs_path: Path,
        size: QSize,
        stamp: int,
        cache_path: Path,
    ) -> None:
        super().__init__()
        self._loader = loader
        self._rel = rel
        self._abs_path = abs_path
        self._size = size
        self._stamp = stamp
        self._cache_path = cache_path

    def run(self) -> None:  # pragma: no cover - executed in worker thread
        image = self._load_image()
        self._loader._delivered.emit(
            self._loader._make_key(self._rel, self._size, self._stamp),
            image,
            self._rel,
        )

    def _load_image(self) -> Optional[QImage]:  # pragma: no cover - worker helper
        target = self._size
        reader = QImageReader(str(self._abs_path))
        reader.setAutoTransform(True)
        original_size = reader.size()
        if original_size.isValid():
            scaled = original_size.scaled(self._size, Qt.KeepAspectRatio)
            if scaled.isValid() and not scaled.isEmpty():
                target = scaled
                reader.setScaledSize(scaled)
        image = reader.read()
        if image.isNull():
            image = self._fallback_heif(target)
            if image is None:
                return None
        canvas = self._composite_canvas(image)
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            if canvas.save(str(tmp_path), "PNG"):
                self._loader._safe_unlink(self._cache_path)
                try:
                    tmp_path.replace(self._cache_path)
                except OSError:
                    # If the cache file is locked, leave the temp file as-is so
                    # the caller still receives an in-memory pixmap. Cleanup is
                    # best effort only.
                    tmp_path.unlink(missing_ok=True)
            else:  # pragma: no cover - Qt returns False on IO errors
                tmp_path.unlink(missing_ok=True)
        except Exception:  # pragma: no cover - cache write failures are non-fatal
            pass
        return canvas

    def _fallback_heif(self, target: QSize) -> Optional[QImage]:  # pragma: no cover - worker helper
        suffix = self._abs_path.suffix.lower()
        if suffix not in {".heic", ".heif", ".heifs", ".heicf"}:
            return None
        try:
            with Image.open(self._abs_path) as img:
                img = ImageOps.exif_transpose(img)
                resample = getattr(Image, "Resampling", Image)
                resample_filter = getattr(resample, "LANCZOS", Image.BICUBIC)
                if target.isValid() and not target.isEmpty():
                    img.thumbnail((target.width(), target.height()), resample_filter)
                qt_image = ImageQt(img.convert("RGBA"))
                return QImage(qt_image)
        except Exception:
            return None

    def _composite_canvas(self, image: QImage) -> QImage:  # pragma: no cover - worker helper
        canvas = QImage(self._size, QImage.Format_ARGB32)
        canvas.fill(QColor("#1b1b1b"))
        scaled = image.scaled(
            self._size,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        target_rect = canvas.rect()
        source_rect = scaled.rect()
        source_rect.moveCenter(target_rect.center())
        painter.drawImage(target_rect, scaled, source_rect)
        painter.end()
        return canvas


class _ThumbnailLoader(QObject):
    """Asynchronous thumbnail renderer with disk and memory caching."""

    ready = Signal(object, str, QPixmap)
    _delivered = Signal(object, object, str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._album_root: Optional[Path] = None
        self._album_root_str: Optional[str] = None
        self._memory: Dict[Tuple[str, str, int, int, int], QPixmap] = {}
        self._pending: Set[Tuple[str, str, int, int, int]] = set()
        self._failures: Set[Tuple[str, str, int, int, int]] = set()
        self._missing: Set[Tuple[str, str, int, int]] = set()
        self._delivered.connect(self._handle_result)

    def reset_for_album(self, root: Path) -> None:
        if self._album_root and self._album_root == root:
            return
        self._album_root = root
        self._album_root_str = str(root.resolve())
        self._memory.clear()
        self._pending.clear()
        self._failures.clear()
        self._missing.clear()

    def request(self, rel: str, path: Path, size: QSize, *, is_image: bool) -> Optional[QPixmap]:
        if self._album_root is None or self._album_root_str is None:
            return None
        base_key = self._base_key(rel, size)
        if not is_image or base_key in self._missing:
            return None
        try:
            stamp = int(path.stat().st_mtime)
        except FileNotFoundError:
            self._missing.add(base_key)
            return None
        key = self._make_key(rel, size, stamp)
        cached = self._memory.get(key)
        if cached is not None:
            return cached
        if key in self._failures:
            return None
        cache_path = self._cache_path(rel, size, stamp)
        if cache_path.exists():
            pixmap = QPixmap(str(cache_path))
            if not pixmap.isNull():
                self._memory[key] = pixmap
                return pixmap
            self._safe_unlink(cache_path)
        if key in self._pending:
            return None
        job = _ThumbnailJob(self, rel, path, size, stamp, cache_path)
        self._pending.add(key)
        self._pool.start(job)
        return None

    def _base_key(self, rel: str, size: QSize) -> Tuple[str, str, int, int]:
        assert self._album_root_str is not None
        return (self._album_root_str, rel, size.width(), size.height())

    def _make_key(self, rel: str, size: QSize, stamp: int) -> Tuple[str, str, int, int, int]:
        base = self._base_key(rel, size)
        return (*base, stamp)

    def _cache_path(self, rel: str, size: QSize, stamp: int) -> Path:
        assert self._album_root is not None
        digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()
        filename = f"{digest}_{stamp}_{size.width()}x{size.height()}.png"
        return self._album_root / WORK_DIR_NAME / "thumbs" / filename

    def _handle_result(
        self,
        key: Tuple[str, str, int, int, int],
        image: Optional[QImage],
        rel: str,
    ) -> None:
        self._pending.discard(key)
        if image is None:
            self._failures.add(key)
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._failures.add(key)
            return
        # Keep only the latest entry for the same asset and size.
        base = key[:-1]
        obsolete = [existing for existing in self._memory if existing[:-1] == base and existing != key]
        for existing in obsolete:
            self._memory.pop(existing, None)
        self._memory[key] = pixmap
        if self._album_root is not None:
            self.ready.emit(self._album_root, rel, pixmap)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        """Best-effort removal of a cache file on all platforms."""

        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            # Windows keeps a handle open when another process (e.g. antivirus or
            # an image previewer) is scanning the file. Mark the file for lazy
            # cleanup by renaming so future attempts use a fresh cache entry.
            try:
                path.rename(path.with_suffix(path.suffix + ".stale"))
            except OSError:
                pass
        except OSError:
            pass



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
        self._row_lookup: Dict[str, int] = {}
        self._thumb_cache: Dict[str, QPixmap] = {}
        self._placeholder_cache: Dict[str, QPixmap] = {}
        self._thumb_size = QSize(192, 192)
        self._thumb_loader = _ThumbnailLoader(self)
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
        self._row_lookup = {row_data["rel"]: idx for idx, row_data in enumerate(payload)}
        # Drop any thumbnails that no longer correspond to a listed asset.
        active = set(self._row_lookup.keys())
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
        placeholder = self._placeholder_for(rel, bool(row.get("is_video")))
        if not self._album_root:
            return placeholder
        abs_path = Path(str(row["abs"]))
        if bool(row.get("is_image")):
            pixmap = self._thumb_loader.request(rel, abs_path, self._thumb_size, is_image=True)
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
