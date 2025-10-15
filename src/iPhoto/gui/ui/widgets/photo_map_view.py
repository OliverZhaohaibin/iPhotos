"""Composite widget that embeds the map preview and renders photo markers."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPen,
    QPixmap,
    QWheelEvent,
    QCloseEvent,
)
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
    screen_x_sum: float = 0.0
    screen_y_sum: float = 0.0

    def __post_init__(self) -> None:
        if not self.assets:
            self.assets.append(self.representative)
        self.latitude_sum = sum(asset.latitude for asset in self.assets)
        self.longitude_sum = sum(asset.longitude for asset in self.assets)
        if self.screen_pos is None:
            self.screen_pos = QPointF()
        count = len(self.assets)
        if count:
            self.screen_x_sum = self.screen_pos.x() * float(count)
            self.screen_y_sum = self.screen_pos.y() * float(count)

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

    def add_asset(
        self,
        asset: GeotaggedAsset,
        projector: Callable[[float, float], Optional[QPointF]] | None = None,
        *,
        projected_point: Optional[QPointF] = None,
    ) -> None:
        """Merge *asset* into the cluster and refresh cached aggregates."""

        self.assets.append(asset)
        self.latitude_sum += asset.latitude
        self.longitude_sum += asset.longitude
        if projected_point is not None:
            # When the caller already computed the projected location in screen
            # space we simply fold it into the running arithmetic mean. This
            # keeps the worker thread independent from the QWidget based map
            # implementation which must only be touched from the GUI thread.
            self.screen_x_sum += projected_point.x()
            self.screen_y_sum += projected_point.y()
            count = float(len(self.assets))
            self.screen_pos = QPointF(self.screen_x_sum / count, self.screen_y_sum / count)
        elif projector is not None:
            self._reproject(projector)

    def _reproject(self, projector: Callable[[float, float], Optional[QPointF]]) -> None:
        """Project the average coordinates back into screen space."""

        point = projector(self.longitude, self.latitude)
        if point is not None:
            self.screen_pos = point
            count = float(len(self.assets))
            self.screen_x_sum = self.screen_pos.x() * count
            self.screen_y_sum = self.screen_pos.y() * count


class _ClusterWorker(QObject):
    """Worker object that performs clustering on a dedicated thread."""

    finished = Signal(int, list)

    TILE_SIZE = 256
    """Base tile size used by the Web Mercator tiling scheme."""

    MERCATOR_LAT_BOUND = 85.05112878
    """Clamp latitude to the numerical range supported by Web Mercator."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._interrupted = False

    def interrupt(self) -> None:
        """Request cancellation of the currently running clustering job."""

        self._interrupted = True

    def build_clusters(
        self,
        request_id: int,
        assets: Sequence[GeotaggedAsset],
        width: int,
        height: int,
        center_x: float,
        center_y: float,
        zoom: float,
        threshold: float,
        cell_size: int,
        margin: int,
    ) -> None:
        """Project *assets* and aggregate them into clusters in screen space.

        The worker mirrors the math performed by :class:`MapWidgetController`
        so the CPU intensive projection loop runs outside of the GUI thread. A
        monotonic ``request_id`` allows :class:`PhotoMapView` to ignore stale
        results when the user has already moved the map.
        """

        self._interrupted = False

        if width <= 0 or height <= 0:
            self.finished.emit(request_id, [])
            return

        world_size = self._world_size(zoom)
        center_px = center_x * world_size
        center_py = center_y * world_size
        top_left_x = center_px - width / 2.0
        top_left_y = center_py - height / 2.0
        half_world = world_size / 2.0

        grid: Dict[tuple[int, int], list[_MarkerCluster]] = {}
        clusters: list[_MarkerCluster] = []

        for asset in assets:
            if self._interrupted:
                return

            point = self._project_to_screen(
                asset.longitude,
                asset.latitude,
                top_left_x,
                top_left_y,
                center_px,
                center_py,
                world_size,
                half_world,
            )

            if point is None:
                continue

            if point.x() < -margin or point.y() < -margin:
                continue
            if point.x() > width + margin or point.y() > height + margin:
                continue

            cell_x = int(point.x() // cell_size)
            cell_y = int(point.y() // cell_size)
            candidates: list[_MarkerCluster] = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    candidates.extend(grid.get((cell_x + dx, cell_y + dy), []))

            assigned = False
            for cluster in candidates:
                if PhotoMapView._distance(cluster.screen_pos, point) <= threshold:
                    cluster.add_asset(asset, projected_point=point)
                    new_cell = (
                        int(cluster.screen_pos.x() // cell_size),
                        int(cluster.screen_pos.y() // cell_size),
                    )
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
                cluster = _MarkerCluster(
                    representative=asset,
                    assets=[asset],
                    screen_pos=point,
                )
                cluster.cell = (cell_x, cell_y)
                cluster.screen_x_sum = point.x()
                cluster.screen_y_sum = point.y()
                clusters.append(cluster)
                grid.setdefault((cell_x, cell_y), []).append(cluster)

        if not self._interrupted:
            self.finished.emit(request_id, clusters)

    def _project_to_screen(
        self,
        lon: float,
        lat: float,
        top_left_x: float,
        top_left_y: float,
        center_px: float,
        center_py: float,
        world_size: float,
        half_world: float,
    ) -> Optional[QPointF]:
        """Convert a geographic coordinate into widget-relative screen space."""

        world_position = self._lonlat_to_world(lon, lat, world_size)
        if world_position is None:
            return None

        world_x, world_y = world_position
        delta_x = world_x - center_px
        if delta_x > half_world:
            world_x -= world_size
        elif delta_x < -half_world:
            world_x += world_size

        screen_x = world_x - top_left_x
        screen_y = world_y - top_left_y
        return QPointF(screen_x, screen_y)

    def _world_size(self, zoom: float) -> float:
        """Return the virtual world edge length in pixels for *zoom*."""

        return float(self.TILE_SIZE * (2.0 ** float(zoom)))

    def _lonlat_to_world(
        self, lon: float, lat: float, world_size: float
    ) -> Optional[tuple[float, float]]:
        """Project GPS coordinates into the continuous Web Mercator plane."""

        try:
            lon = float(lon)
            lat = float(lat)
        except (TypeError, ValueError):
            return None

        lat = max(min(lat, self.MERCATOR_LAT_BOUND), -self.MERCATOR_LAT_BOUND)
        x = (lon + 180.0) / 360.0 * world_size
        sin_lat = math.sin(math.radians(lat))
        y = (
            0.5
            - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)
        ) * world_size
        return x, y


class _MarkerLayer(QWidget):
    """Transparent overlay that paints thumbnails and manages marker clicks."""

    markerActivated = Signal(_MarkerCluster)
    clusterActivated = Signal(_MarkerCluster)

    # ``MARKER_SIZE`` mirrors the historical footprint of the map markers so the
    # overlay does not feel oversized compared to the surrounding UI controls.
    MARKER_SIZE = 72
    # ``THUMBNAIL_NATIVE_SIZE`` describes the cached thumbnail edge length that we
    # request from the shared thumbnail loader. Location previews now rely on the
    # higher fidelity 192x192 assets that power the rest of the application so the
    # map view no longer uses a bespoke, ultra small cache entry.
    THUMBNAIL_NATIVE_SIZE = 192
    # ``THUMBNAIL_DISPLAY_SIZE`` is the actual number of on-screen pixels available
    # within the marker frame for rendering the thumbnail. We keep this at 56px so
    # the markers retain the previous compact appearance while benefiting from the
    # sharper 192px source imagery.
    THUMBNAIL_DISPLAY_SIZE = 56
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
        """Return the native thumbnail resolution requested from the cache."""

        return self.THUMBNAIL_NATIVE_SIZE

    @property
    def thumbnail_display_size(self) -> int:
        """Return the edge length used when painting thumbnails on the map."""

        return self.THUMBNAIL_DISPLAY_SIZE

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
        # ``SmoothPixmapTransform`` guarantees that shrinking the 192px thumbnails
        # down to the 56px display slot uses a high quality resampling filter.
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
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
            display_edge = float(self.THUMBNAIL_DISPLAY_SIZE)
            thumb_rect = QRectF(
                x + (self.MARKER_SIZE - display_edge) / 2.0,
                y + (self.MARKER_SIZE - display_edge) / 2.0,
                display_edge,
                display_edge,
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
        display_size = self.THUMBNAIL_DISPLAY_SIZE
        pixmap = QPixmap(display_size, display_size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor("#cccccc"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, display_size, display_size, 8, 8)
        painter.end()
        return pixmap


class PhotoMapView(QWidget):
    """Embed the map widget and manage geotagged photo markers."""

    assetActivated = Signal(str)
    """Signal emitted when the user activates a single asset marker."""

    _clustering_requested = Signal(int, object, int, int, float, float, float, float, int, int)
    """Internal signal that schedules a clustering job on the worker thread."""

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
        self._view_center_x = 0.5
        self._view_center_y = 0.5
        self._view_zoom = float(self._map_widget.zoom)
        self._is_panning = False
        self._cluster_timer = QTimer(self)
        self._cluster_timer.setSingleShot(True)
        self._cluster_timer.setInterval(80)
        self._cluster_timer.timeout.connect(self._rebuild_clusters)

        self._thumbnail_loader = ThumbnailLoader(self)
        self._thumbnail_loader.ready.connect(self._handle_thumbnail_ready)

        self._map_widget.viewChanged.connect(self._handle_view_changed)
        self._map_widget.panned.connect(self._handle_pan)
        self._map_widget.panFinished.connect(self._handle_pan_finished)
        self._overlay.markerActivated.connect(self._handle_marker_activated)
        self._overlay.clusterActivated.connect(self._handle_cluster_activated)

        # Background clustering infrastructure ---------------------------------
        self._cluster_thread = QThread(self)
        self._cluster_thread.setObjectName("photo-map-cluster-worker")
        self._cluster_worker = _ClusterWorker()
        self._cluster_worker.moveToThread(self._cluster_thread)
        self._clustering_requested.connect(self._cluster_worker.build_clusters)
        self._cluster_worker.finished.connect(self._handle_clustering_finished)
        self._cluster_thread.finished.connect(self._cluster_worker.deleteLater)
        self._cluster_thread.start()
        self._cluster_request_id = 0

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

        if hasattr(self, "_cluster_worker"):
            self._cluster_worker.interrupt()
        if hasattr(self, "_cluster_request_id"):
            self._cluster_request_id += 1
        self._assets = []
        self._clusters = []
        self._is_panning = False
        self._view_center_x = 0.5
        self._view_center_y = 0.5
        self._view_zoom = float(self._map_widget.zoom)
        self._overlay.set_clusters([])

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._overlay.setGeometry(self._map_widget.geometry())
        self._schedule_cluster_update()

    def _schedule_cluster_update(self) -> None:
        if self._is_panning:
            # Panning is handled incrementally by simply shifting the existing
            # markers across the overlay. Deferring the expensive clustering
            # work until the gesture settles keeps the UI responsive.
            return
        self._cluster_timer.start()

    def _handle_view_changed(self, center_x: float, center_y: float, zoom: float) -> None:
        """Record the latest viewport and coalesce clustering updates."""

        self._view_center_x = float(center_x)
        self._view_center_y = float(center_y)
        self._view_zoom = float(zoom)
        self._schedule_cluster_update()

    def _handle_pan(self, delta: QPointF) -> None:
        """Shift visible markers in response to incremental drag updates."""

        self._is_panning = True
        if self._cluster_timer.isActive():
            self._cluster_timer.stop()

        if not self._clusters:
            self._overlay.update()
            return

        for cluster in self._clusters:
            # ``QPointF`` does not support in-place addition in all PySide6
            # builds, therefore both coordinates are updated explicitly.
            cluster.screen_pos = QPointF(
                cluster.screen_pos.x() + delta.x(),
                cluster.screen_pos.y() + delta.y(),
            )
            if getattr(cluster, "bounding_rect", None):
                cluster.bounding_rect.translate(delta.x(), delta.y())

        self._overlay.update()

    def _handle_pan_finished(self) -> None:
        """Resume background clustering once the drag gesture ends."""

        self._is_panning = False
        self._schedule_cluster_update()

    def _rebuild_clusters(self) -> None:
        if not self._assets:
            self._cluster_worker.interrupt()
            self._cluster_request_id += 1
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

        # Cancel any in-flight clustering work before queuing the new job so the
        # worker does not waste time on stale data produced for an outdated
        # viewport.
        self._cluster_worker.interrupt()
        self._cluster_request_id += 1
        request_id = self._cluster_request_id
        self._clustering_requested.emit(
            request_id,
            self._assets,
            width,
            height,
            self._view_center_x,
            self._view_center_y,
            self._view_zoom,
            float(threshold),
            cell_size,
            margin,
        )

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

    def _handle_clustering_finished(self, request_id: int, clusters: list[_MarkerCluster]) -> None:
        """Receive freshly computed clusters from the background worker."""

        if request_id != self._cluster_request_id:
            # ``request_id`` is monotonic so a mismatch means a more recent job
            # already supplied its data.
            return

        self._clusters = clusters
        self._overlay.set_clusters(clusters)
        for cluster in clusters:
            self._ensure_thumbnail(cluster.representative)

    @staticmethod
    def _distance(a: QPointF, b: QPointF) -> float:
        return math.hypot(a.x() - b.x(), a.y() - b.y())

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Ensure the clustering thread shuts down before the widget closes."""

        if self._cluster_thread.isRunning():
            self._cluster_worker.interrupt()
            self._cluster_thread.quit()
            self._cluster_thread.wait()
        super().closeEvent(event)


__all__ = ["PhotoMapView"]
