"""Caching helpers for :class:`AssetListModel`."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap

from ..tasks.thumbnail_loader import ThumbnailLoader
from .live_map import load_live_map


class AssetCacheManager(QObject):
    """Manage thumbnail, placeholder and Live Photo caches."""

    thumbnailReady = Signal(Path, str, QPixmap)

    def __init__(self, thumb_size: QSize, parent: QObject | None = None) -> None:
        """Create an empty cache for thumbnails and transient metadata."""

        super().__init__(parent)
        self._thumb_size = QSize(thumb_size)
        self._thumb_loader = ThumbnailLoader(self)
        self._thumb_loader.ready.connect(self._on_thumb_ready)
        self._thumb_cache: Dict[str, QPixmap] = {}
        self._placeholder_cache: Dict[str, QPixmap] = {}
        self._placeholder_templates: Dict[str, QPixmap] = {}
        self._recently_removed_rows: "OrderedDict[str, Dict[str, object]]" = OrderedDict()
        self._recently_removed_limit = 256
        self._album_root: Optional[Path] = None
        self._live_map: Dict[str, Dict[str, object]] = {}

    def thumbnail_loader(self) -> ThumbnailLoader:
        """Expose the :class:`ThumbnailLoader` used for rendering previews."""

        return self._thumb_loader

    def reset_for_album(self, root: Optional[Path]) -> None:
        """Reset caches so a new album can be loaded."""

        self._album_root = root
        self._thumb_loader.reset_for_album(root)
        self._thumb_cache.clear()
        self._placeholder_cache.clear()
        self._placeholder_templates.clear()
        self._recently_removed_rows.clear()
        self._live_map = {}

    def clear_recently_removed(self) -> None:
        """Drop all cached metadata for recently removed rows."""

        self._recently_removed_rows.clear()

    def set_recently_removed_limit(self, limit: int) -> None:
        """Set the maximum number of cached removal snapshots."""

        self._recently_removed_limit = max(limit, 0)

    def recently_removed(self, absolute_key: str) -> Optional[Dict[str, object]]:
        """Return cached metadata for *absolute_key* if present."""

        return self._recently_removed_rows.get(absolute_key)

    def stash_recently_removed(self, absolute_key: str, metadata: Dict[str, object]) -> None:
        """Store a metadata snapshot for a row that was removed optimistically."""

        self._recently_removed_rows[absolute_key] = dict(metadata)
        self._recently_removed_rows.move_to_end(absolute_key)
        while len(self._recently_removed_rows) > self._recently_removed_limit:
            self._recently_removed_rows.popitem(last=False)

    def remove_recently_removed(self, absolute_key: str) -> None:
        """Discard cached metadata for *absolute_key* if it exists."""

        self._recently_removed_rows.pop(absolute_key, None)

    def thumbnail_for(self, rel: str) -> Optional[QPixmap]:
        """Return the cached thumbnail for *rel*, if available."""

        return self._thumb_cache.get(rel)

    def set_thumbnail(self, rel: str, pixmap: QPixmap) -> None:
        """Store *pixmap* under the cache key *rel*."""

        self._thumb_cache[rel] = pixmap

    def remove_thumbnail(self, rel: str) -> None:
        """Remove the cached thumbnail for *rel* when it exists."""

        self._thumb_cache.pop(rel, None)

    def move_thumbnail(self, old_rel: str, new_rel: str) -> None:
        """Move the cached thumbnail from *old_rel* to *new_rel*."""

        pixmap = self._thumb_cache.pop(old_rel, None)
        if pixmap is not None:
            self._thumb_cache[new_rel] = pixmap

    def clear_thumbnails_not_in(self, active: set[str]) -> None:
        """Discard cached thumbnails whose keys are not present in *active*."""

        self._thumb_cache = {rel: pix for rel, pix in self._thumb_cache.items() if rel in active}

    def clear_all_thumbnails(self) -> None:
        """Remove every cached thumbnail."""

        self._thumb_cache.clear()

    def move_placeholder(self, old_rel: str, new_rel: str) -> None:
        """Move the cached placeholder entry from *old_rel* to *new_rel*."""

        placeholder = self._placeholder_cache.pop(old_rel, None)
        if placeholder is not None:
            self._placeholder_cache[new_rel] = placeholder

    def remove_placeholder(self, rel: str) -> None:
        """Remove the cached placeholder associated with *rel*."""

        self._placeholder_cache.pop(rel, None)

    def clear_placeholders(self) -> None:
        """Erase all cached placeholders so they will be regenerated lazily."""

        self._placeholder_cache.clear()

    def resolve_thumbnail(
        self,
        row: Dict[str, object],
        priority: ThumbnailLoader.Priority,
    ) -> QPixmap:
        """Return a thumbnail for *row*, requesting it asynchronously if needed."""

        rel = str(row["rel"])
        cached = self._thumb_cache.get(rel)
        if cached is not None:
            return cached

        placeholder = self._placeholder_for(rel, bool(row.get("is_video")))
        if not self._album_root:
            return placeholder

        abs_value = row.get("abs", "")
        abs_path = Path(str(abs_value)) if abs_value else self._album_root / rel

        if bool(row.get("is_image")):
            pixmap = self._thumb_loader.request(
                rel,
                abs_path,
                self._thumb_size,
                is_image=True,
                priority=priority,
            )
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
                priority=priority,
            )
            if pixmap is not None:
                self._thumb_cache[rel] = pixmap
                return pixmap

        return placeholder

    def reload_live_metadata(self, rows: List[Dict[str, object]]) -> List[int]:
        """Refresh cached Live Photo metadata and update *rows* in place."""

        if not self._album_root or not rows:
            return []

        live_map = load_live_map(self._album_root)
        self._live_map = dict(live_map)
        updated_rows: List[int] = []
        album_root = self._normalise_path(self._album_root)

        for row_index, row in enumerate(rows):
            rel = str(row.get("rel", ""))
            if not rel:
                continue

            info = self._live_map.get(rel)
            new_is_live = False
            new_motion_rel: Optional[str] = None
            new_motion_abs: Optional[str] = None
            new_group_id: Optional[str] = None

            if isinstance(info, dict):
                group_id = info.get("id")
                if isinstance(group_id, str):
                    new_group_id = group_id
                elif group_id is not None:
                    new_group_id = str(group_id)

                if info.get("role") == "still":
                    motion_rel = info.get("motion")
                    if isinstance(motion_rel, str) and motion_rel:
                        new_motion_rel = motion_rel
                        try:
                            new_motion_abs = str((album_root / motion_rel).resolve())
                        except OSError:
                            new_motion_abs = str(album_root / motion_rel)
                        new_is_live = True

            previous_is_live = bool(row.get("is_live", False))
            previous_motion_rel = row.get("live_motion")
            previous_motion_abs = row.get("live_motion_abs")
            previous_group_id = row.get("live_group_id")

            if (
                previous_is_live == new_is_live
                and (previous_motion_rel or None) == new_motion_rel
                and (previous_motion_abs or None) == new_motion_abs
                and (previous_group_id or None) == new_group_id
            ):
                continue

            row["is_live"] = new_is_live
            row["live_motion"] = new_motion_rel
            row["live_motion_abs"] = new_motion_abs
            row["live_group_id"] = new_group_id
            updated_rows.append(row_index)

        return updated_rows

    def set_live_map(self, mapping: Dict[str, Dict[str, object]]) -> None:
        """Replace the cached Live Photo mapping."""

        self._live_map = dict(mapping)

    def live_map(self) -> Dict[str, Dict[str, object]]:
        """Return the cached Live Photo mapping."""

        return self._live_map

    def _on_thumb_ready(self, root: Path, rel: str, pixmap: QPixmap) -> None:
        """Store thumbnails produced by :class:`ThumbnailLoader` and relay them."""

        if self._album_root and root != self._album_root:
            return
        self._thumb_cache[rel] = pixmap
        self.thumbnailReady.emit(root, rel, pixmap)

    def _placeholder_for(self, rel: str, is_video: bool) -> QPixmap:
        """Return a cached placeholder pixmap for *rel*."""

        cached = self._placeholder_cache.get(rel)
        if cached is not None:
            return cached

        suffix = Path(rel).suffix.lower().lstrip(".")
        if not suffix:
            suffix = "video" if is_video else "media"

        key = f"{suffix}|{is_video}"
        template = self._placeholder_templates.get(key)
        if template is None:
            template = self._create_placeholder(suffix)
            self._placeholder_templates[key] = template

        self._placeholder_cache[rel] = template
        return template

    def _create_placeholder(self, suffix: str) -> QPixmap:
        """Render a text-based placeholder pixmap for *suffix*."""

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

        return canvas

    @staticmethod
    def _normalise_path(path: Path) -> Path:
        """Return a consistently resolved form of *path* for comparisons."""

        try:
            return path.resolve()
        except OSError:
            return path
