"""Composite widget that embeds the map preview and renders photo markers."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from PySide6.QtCore import QCoreApplication, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPaintEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from iPhotos.maps.map_widget.map_widget import MapWidget

from ....library.manager import GeotaggedAsset
from ..tasks.thumbnail_loader import ThumbnailLoader


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
            # ``QPainter.drawPixmap`` requires a QRect when no source rect is provided,
            # therefore the floating point QRectF must be converted to a QRect.
            painter.drawPixmap(thumb_rect.toRect(), thumbnail)
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
            mapped = QMouseEvent(
                event.type(),
                event.position(),
                event.scenePosition(),
                event.globalPosition(),
                event.button(),
                event.buttons(),
                event.modifiers(),
                event.source(),
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
        self._clusters: list[_MarkerCluster] = []
        self._cluster_timer = QTimer(self)
        self._cluster_timer.setSingleShot(True)
        self._cluster_timer.setInterval(80)
        self._cluster_timer.timeout.connect(self._rebuild_clusters)

        self._thumbnail_loader = ThumbnailLoader(self)
        self._thumbnail_loader.ready.connect(self._handle_thumbnail_ready)

        self._map_widget.viewChanged.connect(self._schedule_cluster_update)
        self._overlay.markerActivated.connect(self._handle_marker_activated)
        self._overlay.clusterActivated.connect(self._handle_cluster_activated)

    def map_widget(self) -> MapWidget:
        return self._map_widget

    def set_assets(self, assets: Iterable[GeotaggedAsset], library_root: Path) -> None:
        """Replace the asset catalogue shown on the map."""

        normalized = [asset for asset in assets if isinstance(asset, GeotaggedAsset)]
        self._assets = normalized
        self._library_root = library_root
        self._thumbnail_loader.reset_for_album(library_root)
        self._overlay.clear_pixmaps()
        self._schedule_cluster_update()

    def clear(self) -> None:
        """Remove all markers from the map."""

        self._assets = []
        self._clusters = []
        self._overlay.set_clusters([])

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._overlay.setGeometry(self._map_widget.geometry())
        self._schedule_cluster_update()

    def _schedule_cluster_update(self) -> None:
        self._cluster_timer.start()

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
        if self._library_root is None or root != self._library_root:
            return
        self._overlay.set_thumbnail(rel, pixmap)

    def _handle_marker_activated(self, cluster: _MarkerCluster) -> None:
        asset = cluster.representative
        self.assetActivated.emit(asset.library_relative)

    def _handle_cluster_activated(self, cluster: _MarkerCluster) -> None:
        self._map_widget.focus_on(cluster.longitude, cluster.latitude, zoom_delta=0.8)

    @staticmethod
    def _distance(a: QPointF, b: QPointF) -> float:
        return math.hypot(a.x() - b.x(), a.y() - b.y())


__all__ = ["PhotoMapView"]
