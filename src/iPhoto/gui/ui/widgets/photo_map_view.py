"""Composite widget that embeds the map preview and renders photo markers."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

from PySide6.QtCore import QCoreApplication, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPaintEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from iPhotos.maps.map_widget.map_widget import MapWidget

from ....config import WORK_DIR_NAME
from ....library.manager import GeotaggedAsset
from ..tasks.thumbnail_loader import ThumbnailLoader

_THUMBNAIL_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".avif",
    ".jfif",
    ".heic",
    ".heif",
)


@dataclass
class _MarkerCluster:
    """Group of assets collapsed into a single marker on the map."""

    representative: GeotaggedAsset
    assets: list[GeotaggedAsset] = field(default_factory=list)
    latitude_sum: float = 0.0
    longitude_sum: float = 0.0
    screen_pos: QPointF = field(default_factory=QPointF)
    cell: tuple[int, int] | None = None
    bounding_rect: QRectF = field(default_factory=QRectF)

    def __post_init__(self) -> None:
        if not self.assets:
            self.assets.append(self.representative)
        self.latitude_sum = sum(asset.latitude for asset in self.assets)
        self.longitude_sum = sum(asset.longitude for asset in self.assets)

    @property
    def latitude(self) -> float:
        """Return the average latitude for the cluster."""

        count = len(self.assets) or 1
        return self.latitude_sum / count

    @property
    def longitude(self) -> float:
        """Return the average longitude for the cluster."""

        count = len(self.assets) or 1
        return self.longitude_sum / count

    def add_asset(self, asset: GeotaggedAsset, projector: Callable[[float, float], Optional[QPointF]]) -> None:
        """Merge *asset* into the cluster and refresh the projected position."""

        self.assets.append(asset)
        self.latitude_sum += asset.latitude
        self.longitude_sum += asset.longitude
        self._reproject(projector)

    def _reproject(self, projector: Callable[[float, float], Optional[QPointF]]) -> None:
        """Project the average coordinates back into screen space."""

        point = projector(self.longitude, self.latitude)
        if point is not None:
            self.screen_pos = point


class _MarkerLayer(QWidget):
    """Transparent overlay that paints thumbnails and manages marker clicks."""

    markerActivated = Signal(_MarkerCluster)
    clusterActivated = Signal(_MarkerCluster)

    MARKER_SIZE = 72
    THUMBNAIL_SIZE = 56
    BADGE_DIAMETER = 26

    def __init__(self, map_widget: MapWidget, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._map_widget = map_widget
        self._clusters: list[_MarkerCluster] = []
        self._pixmaps: Dict[str, QPixmap] = {}
        self._placeholder = self._create_placeholder()
        self._badge_font = QFont()
        self._badge_font.setBold(True)
        self._badge_pen = QPen(QColor("white"))
        self._badge_pen.setWidth(1)
        self._badge_brush = QColor("#d64541")

    @property
    def marker_size(self) -> int:
        return self.MARKER_SIZE

    @property
    def thumbnail_size(self) -> int:
        return self.THUMBNAIL_SIZE

    def set_clusters(self, clusters: Sequence[_MarkerCluster]) -> None:
        """Replace the rendered clusters and schedule a repaint."""

        self._clusters = list(clusters)
        self.update()

    def set_thumbnail(self, rel: str, pixmap: QPixmap) -> None:
        """Cache the pixmap associated with *rel* and refresh the overlay."""

        if pixmap.isNull():
            return
        self._pixmaps[rel] = pixmap
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        for cluster in self._clusters:
            self._paint_cluster(painter, cluster)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._handle_mouse(event):
            event.accept()
            return
        self._forward_event(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._handle_mouse(event):
            event.accept()
            return
        self._forward_event(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._handle_mouse(event, dispatch=False):
            event.accept()
            return
        self._forward_event(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        self._forward_event(event)

    def clear_pixmaps(self) -> None:
        """Drop cached pixmaps so outdated thumbnails are not reused."""

        self._pixmaps.clear()
        self.update()

    def _paint_cluster(self, painter: QPainter, cluster: _MarkerCluster) -> None:
        half = self.MARKER_SIZE / 2.0
        x = cluster.screen_pos.x() - half
        y = cluster.screen_pos.y() - half
        rect = QRectF(x, y, self.MARKER_SIZE, self.MARKER_SIZE)

        painter.setPen(QPen(QColor(0, 0, 0, 80), 2))
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.drawRoundedRect(rect, 8, 8)

        thumbnail = self._pixmaps.get(cluster.representative.library_relative)
        if thumbnail is None:
            thumbnail = self._placeholder
        if not thumbnail.isNull():
            thumb_rect = QRectF(
                x + (self.MARKER_SIZE - self.THUMBNAIL_SIZE) / 2.0,
                y + (self.MARKER_SIZE - self.THUMBNAIL_SIZE) / 2.0,
                self.THUMBNAIL_SIZE,
                self.THUMBNAIL_SIZE,
            )
            source_rect = QRectF(thumbnail.rect())
            # ``QPainter.drawPixmap`` only accepts a ``QRectF`` target if a matching source
            # rectangle is supplied. By passing the pixmap's full bounds as the source we keep
            # floating point precision for the marker placement while satisfying the overload
            # requirements.
            painter.drawPixmap(thumb_rect, thumbnail, source_rect)

        count = len(cluster.assets)
        if count > 1:
            badge_rect = QRectF(
                rect.right() - self.BADGE_DIAMETER + 4,
                rect.top() - 4,
                self.BADGE_DIAMETER,
                self.BADGE_DIAMETER,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._badge_brush)
            painter.drawEllipse(badge_rect)
            painter.setPen(self._badge_pen)
            painter.setFont(self._badge_font)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, str(count))

        cluster.bounding_rect = rect

    def _handle_mouse(self, event: QMouseEvent, *, dispatch: bool = True) -> bool:
        position = event.position()
        for cluster in reversed(self._clusters):
            if getattr(cluster, "bounding_rect", QRectF()).contains(position):
                if not dispatch:
                    return True
                if len(cluster.assets) == 1:
                    self.markerActivated.emit(cluster)
                else:
                    self.clusterActivated.emit(cluster)
                return True
        return False

    def _forward_event(self, event: QMouseEvent | QWheelEvent) -> None:
        if isinstance(event, QWheelEvent):
            mapped = QWheelEvent(
                event.position(),
                event.globalPosition(),
                event.pixelDelta(),
                event.angleDelta(),
                event.buttons(),
                event.modifiers(),
                event.phase(),
                event.inverted(),
                event.source(),
            )
        elif isinstance(event, QMouseEvent):
            # ``QMouseEvent`` exposes several constructor overloads.  The variant
            # used here mirrors the object that Qt delivered to the overlay
            # without forwarding the scene coordinates that only exist for
            # graphics view events.  Supplying the lean overload prevents Qt
            # from raising a ``TypeError`` when the event is reposted to the
            # map widget.
            mapped = QMouseEvent(
                event.type(),
                event.position(),
                event.globalPosition(),
                event.button(),
                event.buttons(),
                event.modifiers(),
            )
        else:
            return
        QCoreApplication.postEvent(self._map_widget, mapped)

    def _create_placeholder(self) -> QPixmap:
        pixmap = QPixmap(self.THUMBNAIL_SIZE, self.THUMBNAIL_SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor("#cccccc"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, self.THUMBNAIL_SIZE, self.THUMBNAIL_SIZE, 8, 8)
        painter.end()
        return pixmap


class PhotoMapView(QWidget):
    """Embed the map widget and manage geotagged photo markers."""

    assetActivated = Signal(str)
    """Signal emitted when the user activates a single asset marker."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._map_widget = MapWidget(self)
        layout.addWidget(self._map_widget)

        self._overlay = _MarkerLayer(self._map_widget, self)
        self._overlay.setGeometry(self._map_widget.geometry())
        self._overlay.raise_()

        self._assets: list[GeotaggedAsset] = []
        self._library_root: Optional[Path] = None
        self._library_root_token: Optional[str] = None
        self._clusters: list[_MarkerCluster] = []
        self._pending_focus: bool = False
        self._cluster_timer = QTimer(self)
        self._cluster_timer.setSingleShot(True)
        self._cluster_timer.setInterval(80)
        self._cluster_timer.timeout.connect(self._rebuild_clusters)

        self._thumbnail_loader = ThumbnailLoader(self)
        self._thumbnail_loader.ready.connect(self._handle_thumbnail_ready)

        # ``_prebaked_thumbnail_tokens`` maps a variety of lookup tokens to
        # on-disk thumbnail files discovered inside ``.iPhoto/thumbs``.  The
        # dictionary lets us satisfy future lookups in constant time instead of
        # walking the filesystem for every marker.
        self._prebaked_thumbnail_tokens: Dict[str, Path] = {}
        # ``_prebaked_pixmaps`` caches scaled ``QPixmap`` instances derived
        # from pre-rendered thumbnails so the overlay does not repeatedly load
        # them from disk.
        self._prebaked_pixmaps: Dict[str, QPixmap] = {}
        # ``_prebaked_misses`` tracks assets that have no matching thumbnail
        # inside ``.iPhoto/thumbs``.  Recording the miss avoids redundant
        # filesystem probes for assets that definitely require the async
        # loader.
        self._prebaked_misses: Set[str] = set()

        self._map_widget.viewChanged.connect(self._schedule_cluster_update)
        self._overlay.markerActivated.connect(self._handle_marker_activated)
        self._overlay.clusterActivated.connect(self._handle_cluster_activated)

    def map_widget(self) -> MapWidget:
        return self._map_widget

    def set_assets(self, assets: Iterable[GeotaggedAsset], library_root: Path) -> None:
        """Replace the asset catalogue shown on the map."""

        normalized = [asset for asset in assets if isinstance(asset, GeotaggedAsset)]
        self._assets = normalized
        normalized_root = self._normalize_root(library_root)
        self._library_root = normalized_root
        self._library_root_token = self._root_token(normalized_root)
        self._thumbnail_loader.reset_for_album(normalized_root)
        self._overlay.clear_pixmaps()
        self._prebaked_pixmaps.clear()
        self._prebaked_misses.clear()
        self._index_prebaked_thumbnails(self._assets)
        self._schedule_cluster_update()
        self._pending_focus = bool(self._assets)
        self._focus_on_assets()

    def clear(self) -> None:
        """Remove all markers from the map."""

        self._assets = []
        self._clusters = []
        self._overlay.set_clusters([])
        self._pending_focus = False
        self._library_root = None
        self._library_root_token = None
        self._prebaked_thumbnail_tokens.clear()
        self._prebaked_pixmaps.clear()
        self._prebaked_misses.clear()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._overlay.setGeometry(self._map_widget.geometry())
        self._schedule_cluster_update()
        if self._pending_focus:
            self._focus_on_assets()

    def _schedule_cluster_update(self) -> None:
        self._cluster_timer.start()

    def _focus_on_assets(self) -> None:
        """Centre the map on the current assets once geometry information exists."""

        if not self._pending_focus or not self._assets:
            return

        width = self._map_widget.width()
        height = self._map_widget.height()
        if width <= 0 or height <= 0:
            # ``set_assets`` may run before Qt finalises the widget geometry.
            # Deferring the zoom calculation until a resize event arrives keeps
            # us from dividing by zero while still guaranteeing a later retry.
            return

        latitudes = [asset.latitude for asset in self._assets]
        longitudes = [asset.longitude for asset in self._assets]
        center_lat = sum(latitudes) / len(latitudes)
        center_lon = sum(longitudes) / len(longitudes)

        self._map_widget.center_on(center_lon, center_lat)
        zoom = self._estimate_zoom_for_assets(longitudes, latitudes, width, height)
        self._map_widget.set_zoom(zoom)
        self._pending_focus = False

    def _rebuild_clusters(self) -> None:
        if not self._assets:
            self._clusters = []
            self._overlay.set_clusters([])
            return

        size = self._map_widget.size()
        if size.isEmpty():
            return
        threshold = max(self._overlay.marker_size * 0.6, 48)
        cell_size = max(int(threshold), 1)
        width = size.width()
        height = size.height()
        margin = self._overlay.marker_size

        def projector(lon: float, lat: float) -> Optional[QPointF]:
            return self._map_widget.project_lonlat(lon, lat)

        grid: Dict[tuple[int, int], list[_MarkerCluster]] = {}
        clusters: list[_MarkerCluster] = []

        for asset in self._assets:
            point = projector(asset.longitude, asset.latitude)
            if point is None:
                continue
            if point.x() < -margin or point.y() < -margin:
                continue
            if point.x() > width + margin or point.y() > height + margin:
                continue

            cell_x = int(point.x() // cell_size)
            cell_y = int(point.y() // cell_size)
            candidates = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    candidates.extend(grid.get((cell_x + dx, cell_y + dy), []))

            assigned = False
            for cluster in candidates:
                if self._distance(cluster.screen_pos, point) <= threshold:
                    cluster.add_asset(asset, projector)
                    new_cell = int(cluster.screen_pos.x() // cell_size), int(cluster.screen_pos.y() // cell_size)
                    if cluster.cell != new_cell:
                        if cluster.cell in grid:
                            try:
                                grid[cluster.cell].remove(cluster)
                            except ValueError:
                                pass
                        grid.setdefault(new_cell, []).append(cluster)
                        cluster.cell = new_cell
                    assigned = True
                    break

            if not assigned:
                cluster = _MarkerCluster(representative=asset, assets=[asset], screen_pos=point)
                cluster.cell = (cell_x, cell_y)
                clusters.append(cluster)
                grid.setdefault((cell_x, cell_y), []).append(cluster)

        self._clusters = clusters
        self._overlay.set_clusters(clusters)
        for cluster in clusters:
            self._ensure_thumbnail(cluster.representative)

    def _ensure_thumbnail(self, asset: GeotaggedAsset) -> None:
        if self._library_root is None:
            return
        size = QSize(self._overlay.thumbnail_size, self._overlay.thumbnail_size)
        prebaked = self._resolve_prebaked_thumbnail(asset, size)
        if prebaked is not None:
            self._overlay.set_thumbnail(asset.library_relative, prebaked)
            return
        # ``print`` statements below intentionally surface the thumbnail flow in developer
        # consoles.  The messages help us determine whether requests are dispatched and
        # whether callbacks are discarded because of root mismatches when debugging why
        # nothing appears on the map.
        print(
            "[PhotoMapView] Requesting thumbnail:",
            f"root={self._library_root}",
            f"rel={asset.library_relative}",
            f"abs={asset.absolute_path}",
        )
        pixmap = self._thumbnail_loader.request(
            asset.library_relative,
            asset.absolute_path,
            size,
            is_image=asset.is_image,
            is_video=asset.is_video,
            still_image_time=asset.still_image_time,
            duration=asset.duration,
        )
        if pixmap is not None:
            self._overlay.set_thumbnail(asset.library_relative, pixmap)

    def _handle_thumbnail_ready(self, root: Path, rel: str, pixmap: QPixmap) -> None:
        if self._library_root is None or self._library_root_token is None:
            return

        incoming_root = self._normalize_root(root)
        incoming_token = self._root_token(incoming_root)
        if incoming_token != self._library_root_token:
            print(
                "[PhotoMapView] Ignoring thumbnail with mismatched root:",
                f"incoming={incoming_root}",
                f"token={incoming_token}",
                f"expected={self._library_root_token}",
            )
            return
        # ``print`` is intentionally used instead of the logging module so the
        # thumbnail path appears immediately in developer consoles when
        # diagnosing why map thumbnails are missing.  The path is reconstructed
        # from the loader's ``root`` and ``rel`` arguments so the message is
        # unambiguous on every platform.
        print(f"[PhotoMapView] Thumbnail ready: {incoming_root / rel}")
        self._overlay.set_thumbnail(rel, pixmap)

    def _resolve_prebaked_thumbnail(self, asset: GeotaggedAsset, size: QSize) -> Optional[QPixmap]:
        """Return a marker-sized pixmap derived from pre-rendered thumbnails.

        The desktop importer ships with thumbnails inside ``.iPhoto/thumbs``.
        When those files exist we can bypass the asynchronous loader entirely,
        which is crucial on systems lacking the codecs required to generate
        new pixmaps (for example HEIC/HEVC support on Windows).
        """

        rel = asset.library_relative
        cached = self._prebaked_pixmaps.get(rel)
        if cached is not None:
            return cached
        if rel in self._prebaked_misses:
            return None

        thumb_path = self._find_prebaked_thumbnail_path(asset)
        if thumb_path is None:
            self._prebaked_misses.add(rel)
            return None

        pixmap = QPixmap(str(thumb_path))
        if pixmap.isNull():
            self._prebaked_misses.add(rel)
            return None

        composed = self._composite_marker_pixmap(pixmap, size)
        self._prebaked_pixmaps[rel] = composed
        print(f"[PhotoMapView] Using cached thumbnail: {thumb_path}")
        return composed

    def _find_prebaked_thumbnail_path(self, asset: GeotaggedAsset) -> Optional[Path]:
        """Return the file path of a pre-rendered thumbnail if one exists."""

        if not self._prebaked_thumbnail_tokens:
            return None

        rel_path = Path(asset.album_relative)
        tokens: List[str] = []

        def register(candidate: str) -> None:
            if candidate:
                tokens.append(candidate)

        posix_rel = rel_path.as_posix()
        register(posix_rel)
        register(posix_rel.lower())
        register(asset.library_relative)
        register(asset.library_relative.lower())
        register(rel_path.name)
        register(rel_path.name.lower())
        register(rel_path.stem)
        register(rel_path.stem.lower())
        flattened = posix_rel.replace("/", "_")
        register(flattened)
        register(flattened.lower())
        flattened_backslash = posix_rel.replace("/", "\\")
        register(flattened_backslash)
        register(flattened_backslash.lower())
        register(asset.asset_id)
        register(asset.asset_id.lower())
        if asset.asset_id.startswith("as_"):
            short_id = asset.asset_id[3:]
            register(short_id)
            register(short_id.lower())

        for ext in _THUMBNAIL_EXTENSIONS:
            register(f"{posix_rel}{ext}")
            register(f"{posix_rel.lower()}{ext}")
            register(f"{rel_path.name}{ext}")
            register(f"{rel_path.name.lower()}{ext}")
            register(f"{rel_path.stem}{ext}")
            register(f"{rel_path.stem.lower()}{ext}")
            register(f"{asset.asset_id}{ext}")
            register(f"{asset.asset_id.lower()}{ext}")

        seen: Set[str] = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            candidate = self._prebaked_thumbnail_tokens.get(token)
            if candidate is not None and candidate.exists():
                return candidate
        return None

    def _composite_marker_pixmap(self, pixmap: QPixmap, size: QSize) -> QPixmap:
        """Scale/crop *pixmap* so it fills the square marker slot."""

        if pixmap.size() == size:
            return pixmap

        canvas = QPixmap(size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing, True)

        scaled = pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        source_rect = QRectF(0.0, 0.0, scaled.width(), scaled.height())
        target_rect = QRectF(0.0, 0.0, size.width(), size.height())

        if scaled.width() > size.width():
            diff = scaled.width() - size.width()
            source_rect.adjust(diff / 2.0, 0.0, -diff / 2.0, 0.0)
        if scaled.height() > size.height():
            diff = scaled.height() - size.height()
            source_rect.adjust(0.0, diff / 2.0, 0.0, -diff / 2.0)

        painter.drawPixmap(target_rect, scaled, source_rect)
        painter.end()
        return canvas

    def _index_prebaked_thumbnails(self, assets: Sequence[GeotaggedAsset]) -> None:
        """Populate the token→path index for ``.iPhoto/thumbs`` caches."""

        tokens: Dict[str, Path] = {}
        visited: Set[Path] = set()

        for asset in assets:
            thumb_dir = asset.album_path / WORK_DIR_NAME / "thumbs"
            if thumb_dir in visited:
                continue
            visited.add(thumb_dir)
            if not thumb_dir.exists():
                continue
            try:
                candidates = thumb_dir.rglob("*")
            except OSError:
                continue
            for path in candidates:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in _THUMBNAIL_EXTENSIONS:
                    continue
                rel = path.relative_to(thumb_dir)
                rel_posix = rel.as_posix()
                # Each thumbnail registers multiple lookup tokens so we can
                # match assets regardless of whether the cache mirrors the
                # original filename, flattens it, or prefixes hashes/sizes.
                entries: Set[str] = {
                    rel_posix,
                    rel_posix.lower(),
                    path.name,
                    path.name.lower(),
                    path.stem,
                    path.stem.lower(),
                }
                without_ext = rel.with_suffix("")
                without_ext_posix = without_ext.as_posix()
                entries.add(without_ext_posix)
                entries.add(without_ext_posix.lower())
                entries.add(without_ext.name)
                entries.add(without_ext.name.lower())
                flattened = rel_posix.replace("/", "_")
                entries.add(flattened)
                entries.add(flattened.lower())
                flattened_backslash = rel_posix.replace("/", "\\")
                entries.add(flattened_backslash)
                entries.add(flattened_backslash.lower())
                for entry in list(entries):
                    entries.add(entry.strip())
                for entry in entries:
                    tokens.setdefault(entry, path)

        self._prebaked_thumbnail_tokens = tokens

    def _handle_marker_activated(self, cluster: _MarkerCluster) -> None:
        asset = cluster.representative
        self.assetActivated.emit(asset.library_relative)

    def _handle_cluster_activated(self, cluster: _MarkerCluster) -> None:
        self._map_widget.focus_on(cluster.longitude, cluster.latitude, zoom_delta=0.8)

    @staticmethod
    def _distance(a: QPointF, b: QPointF) -> float:
        return math.hypot(a.x() - b.x(), a.y() - b.y())

    @staticmethod
    def _normalize_root(root: Path | str) -> Path:
        """Return *root* as an absolute :class:`Path` without resolving failures."""

        candidate = Path(root)
        try:
            expanded = candidate.expanduser()
        except RuntimeError:
            expanded = candidate
        try:
            return expanded.resolve(strict=False)
        except OSError:
            # ``resolve(strict=False)`` may still fail on certain network volumes on
            # Windows.  Falling back to ``absolute()`` keeps the comparison stable
            # while avoiding an exception that would prevent thumbnails from
            # loading entirely.
            return expanded.absolute()

    @staticmethod
    def _root_token(root: Path) -> str:
        """Return a normalised identity string used for root comparisons."""

        # Using ``normcase`` mirrors the platform's path comparison semantics.
        # On Windows this collapses case differences (``C:\`` vs ``c:\``),
        # which otherwise prevent thumbnails from being accepted because the
        # loader emits the resolved drive letter.  On POSIX platforms the value
        # is returned unchanged.
        return os.path.normcase(str(root))

    @staticmethod
    def _mercator_y(lat: float) -> float:
        """Convert *lat* to the Web Mercator Y coordinate used for zoom math."""

        clamped = max(min(lat, 89.9), -89.9)
        radians = math.radians(clamped)
        return math.log(math.tan(math.pi / 4.0 + radians / 2.0))

    @classmethod
    def _estimate_zoom_for_assets(
        cls,
        longitudes: Sequence[float],
        latitudes: Sequence[float],
        width: int,
        height: int,
    ) -> float:
        """Return a zoom that keeps every asset inside the widget bounds."""

        if not longitudes or not latitudes:
            return 2.0

        lon_min = min(longitudes)
        lon_max = max(longitudes)
        lat_min = min(latitudes)
        lat_max = max(latitudes)

        lon_span = max(lon_max - lon_min, 1e-6)
        mercator_span = max(cls._mercator_y(lat_max) - cls._mercator_y(lat_min), 1e-6)

        width = max(width, 1)
        height = max(height, 1)

        zoom_x = math.log2(width * 360.0 / (256.0 * lon_span))
        zoom_y = math.log2(height * (2.0 * math.pi) / (256.0 * mercator_span))

        target_zoom = min(zoom_x, zoom_y) - 0.5
        # ``MapWidgetController`` clamps zoom to ``[0, 8]`` so we mirror the
        # bounds here.  ``max``/``min`` also remove any stray ``nan`` results in
        # degenerate cases.
        return max(0.0, min(8.0, target_zoom))


__all__ = ["PhotoMapView"]
