"""Asynchronous thumbnail rendering helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from PySide6.QtCore import QObject, QRunnable, QSize, QThreadPool, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap
from shiboken6 import Shiboken

from ....config import WORK_DIR_NAME
from ...utils import image_loader
from .video_frame_grabber import grab_video_frame


class ThumbnailJob(QRunnable):
    """Background task that renders a thumbnail ``QImage``."""

    def __init__(
        self,
        loader: "ThumbnailLoader",
        rel: str,
        abs_path: Path,
        size: QSize,
        stamp: int,
        cache_path: Path,
        *,
        is_image: bool,
        is_video: bool,
        still_image_time: Optional[float],
        duration: Optional[float],
    ) -> None:
        super().__init__()
        self._loader = loader
        self._rel = rel
        self._abs_path = abs_path
        self._size = size
        self._stamp = stamp
        self._cache_path = cache_path
        self._is_image = is_image
        self._is_video = is_video
        self._still_image_time = still_image_time
        self._duration = duration

    def run(self) -> None:  # pragma: no cover - executed in worker thread
        image = self._render_media()
        if image is not None:
            self._write_cache(image)
        if not Shiboken.isValid(self._loader):  # pragma: no cover - loader destroyed mid-job
            return
        try:
            self._loader._delivered.emit(
                self._loader._make_key(self._rel, self._size, self._stamp),
                image,
                self._rel,
            )
        except RuntimeError:  # pragma: no cover - race with QObject deletion
            pass

    def _render_media(self) -> Optional[QImage]:  # pragma: no cover - worker helper
        if self._is_video:
            return self._render_video()
        if self._is_image:
            return self._render_image()
        return None

    def _render_image(self) -> Optional[QImage]:  # pragma: no cover - worker helper
        image = image_loader.load_qimage(self._abs_path, self._size)
        if image is None:
            return None
        return self._composite_canvas(image)

    def _render_video(self) -> Optional[QImage]:  # pragma: no cover - worker helper
        image = grab_video_frame(
            self._abs_path,
            self._size,
            still_image_time=self._still_image_time,
            duration=self._duration,
        )
        if image is None:
            return None
        return self._composite_canvas(image)

    def _composite_canvas(self, image: QImage) -> QImage:  # pragma: no cover - worker helper
        canvas = QImage(self._size, QImage.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.transparent)
        scaled = image.scaled(
            self._size,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        target_rect = canvas.rect()
        source_rect = scaled.rect()
        if source_rect.width() > target_rect.width():
            diff = source_rect.width() - target_rect.width()
            left = diff // 2
            right = diff - left
            source_rect.adjust(left, 0, -right, 0)
        if source_rect.height() > target_rect.height():
            diff = source_rect.height() - target_rect.height()
            top = diff // 2
            bottom = diff - top
            source_rect.adjust(0, top, 0, -bottom)
        painter.drawImage(target_rect, scaled, source_rect)
        painter.end()
        return canvas

    def _write_cache(self, canvas: QImage) -> None:  # pragma: no cover - worker helper
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            if canvas.save(str(tmp_path), "PNG"):
                ThumbnailLoader._safe_unlink(self._cache_path)
                try:
                    tmp_path.replace(self._cache_path)
                except OSError:
                    tmp_path.unlink(missing_ok=True)
            else:  # pragma: no cover - Qt returns False on IO errors
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


class ThumbnailLoader(QObject):
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

    def request(
        self,
        rel: str,
        path: Path,
        size: QSize,
        *,
        is_image: bool,
        is_video: bool = False,
        still_image_time: Optional[float] = None,
        duration: Optional[float] = None,
    ) -> Optional[QPixmap]:
        if self._album_root is None or self._album_root_str is None:
            return None
        base_key = self._base_key(rel, size)
        if base_key in self._missing:
            return None
        if not is_image and not is_video:
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
        job = ThumbnailJob(
            self,
            rel,
            path,
            size,
            stamp,
            cache_path,
            is_image=is_image,
            is_video=is_video,
            still_image_time=still_image_time,
            duration=duration,
        )
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
        base = key[:-1]
        obsolete = [existing for existing in self._memory if existing[:-1] == base and existing != key]
        for existing in obsolete:
            self._memory.pop(existing, None)
        self._memory[key] = pixmap
        if self._album_root is not None:
            self.ready.emit(self._album_root, rel, pixmap)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            try:
                path.rename(path.with_suffix(path.suffix + ".stale"))
            except OSError:
                pass
        except OSError:
            pass
