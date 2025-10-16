"""Controller that manages clustering and interaction for map markers."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, QThread, QTimer, Signal
from PySide6.QtGui import QPixmap

from iPhotos.maps.map_widget.map_gl_widget import MapGLWidget
from iPhotos.maps.map_widget.map_widget import MapWidget
from iPhotos.maps.map_widget.map_renderer import CityAnnotation

from ....library.manager import GeotaggedAsset
from ..tasks.thumbnail_loader import ThumbnailLoader


@dataclass
class _MarkerCluster:
    """Aggregate of geotagged assets rendered as a single marker."""

    representative: GeotaggedAsset
    assets: list[GeotaggedAsset] = field(default_factory=list)
    latitude_sum: float = 0.0
    longitude_sum: float = 0.0
    screen_pos: QPointF = field(default_factory=QPointF)
    screen_x_sum: float = 0.0
    screen_y_sum: float = 0.0
    bounding_rect: QRectF = field(default_factory=QRectF)

    def __post_init__(self) -> None:
        """Prime the cached aggregates once the dataclass is initialised."""

        if not self.assets:
            self.assets.append(self.representative)
        self.latitude_sum = sum(asset.latitude for asset in self.assets)
        self.longitude_sum = sum(asset.longitude for asset in self.assets)
        count = len(self.assets)
        if count:
            self.screen_x_sum = self.screen_pos.x() * float(count)
            self.screen_y_sum = self.screen_pos.y() * float(count)

    @property
    def latitude(self) -> float:
        """Return the average latitude represented by the cluster."""

        count = len(self.assets) or 1
        return self.latitude_sum / count

    @property
    def longitude(self) -> float:
        """Return the average longitude represented by the cluster."""

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
            count = float(len(self.assets))
            self.screen_x_sum += projected_point.x()
            self.screen_y_sum += projected_point.y()
            self.screen_pos = QPointF(self.screen_x_sum / count, self.screen_y_sum / count)
        elif projector is not None:
            self._reproject(projector)

    def _reproject(self, projector: Callable[[float, float], Optional[QPointF]]) -> None:
        """Project the average coordinate back into widget space."""

        point = projector(self.longitude, self.latitude)
        if point is None:
            return
        self.screen_pos = point
        count = float(len(self.assets))
        self.screen_x_sum = point.x() * count
        self.screen_y_sum = point.y() * count


class _ClusterWorker(QObject):
    """Worker object that performs clustering on a dedicated thread."""

    finished = Signal(int, list)

    TILE_SIZE = 256
    MERCATOR_LAT_BOUND = 85.05112878

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
        """Project *assets* and aggregate them into clusters in screen space."""

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
                if MarkerController.distance(cluster.screen_pos, point) <= threshold:
                    cluster.add_asset(asset, projected_point=point)
                    new_cell = (
                        int(cluster.screen_pos.x() // cell_size),
                        int(cluster.screen_pos.y() // cell_size),
                    )
                    if getattr(cluster, "cell", None) != new_cell:
                        old_cell = getattr(cluster, "cell", None)
                        if old_cell in grid:
                            try:
                                grid[old_cell].remove(cluster)
                            except ValueError:
                                pass
                        grid.setdefault(new_cell, []).append(cluster)
                        cluster.cell = new_cell  # type: ignore[attr-defined]
                    assigned = True
                    break

            if not assigned:
                cluster = _MarkerCluster(
                    representative=asset,
                    assets=[asset],
                    screen_pos=point,
                )
                cluster.cell = (cell_x, cell_y)  # type: ignore[attr-defined]
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
        return float(self.TILE_SIZE * (2.0 ** float(zoom)))

    def _lonlat_to_world(
        self, lon: float, lat: float, world_size: float
    ) -> Optional[tuple[float, float]]:
        try:
            lon = float(lon)
            lat = float(lat)
        except (TypeError, ValueError):
            return None

        lat = max(min(lat, self.MERCATOR_LAT_BOUND), -self.MERCATOR_LAT_BOUND)
        x = (lon + 180.0) / 360.0 * world_size
        sin_lat = math.sin(math.radians(lat))
        y = (
            0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)
        ) * world_size
        return x, y


class MarkerController(QObject):
    """Encapsulates marker state, clustering and event handling."""

    clustersUpdated = Signal(list)
    citiesUpdated = Signal(list)
    assetActivated = Signal(str)
    thumbnailUpdated = Signal(str, QPixmap)
    thumbnailsInvalidated = Signal()
    _clustering_requested = Signal(int, object, int, int, float, float, float, float, int, int)

    # ``CITY_LABEL_FETCH_LEVEL`` mirrors the map renderer's tile pyramid.  When
    # the integer fetch level meets or exceeds this constant (i.e. zooming to
    # levels 5 and 6 on the bundled 0–6 stack) the controller publishes city
    # annotations so the renderer can draw lightweight labels alongside the
    # always-on photo clusters.
    CITY_LABEL_FETCH_LEVEL = 5

    def __init__(
        self,
        map_widget: MapWidget | MapGLWidget,
        thumbnail_loader: ThumbnailLoader,
        *,
        marker_size: int,
        thumbnail_size: int,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._map_widget = map_widget
        self._thumbnail_loader = thumbnail_loader
        self._marker_size = int(marker_size)
        self._thumbnail_size = int(thumbnail_size)
        self._assets: list[GeotaggedAsset] = []
        self._library_root: Optional[Path] = None
        self._clusters: list[_MarkerCluster] = []
        self._city_annotations: list[CityAnnotation] = []
        self._view_center_x = 0.5
        self._view_center_y = 0.5
        self._view_zoom = float(self._map_widget.zoom)
        self._is_panning = False

        self._cluster_timer = QTimer(self)
        self._cluster_timer.setSingleShot(True)
        self._cluster_timer.setInterval(80)
        self._cluster_timer.timeout.connect(self._rebuild_clusters)

        self._cluster_thread = QThread(self)
        self._cluster_thread.setObjectName("photo-map-cluster-worker")
        self._cluster_worker = _ClusterWorker()
        self._cluster_worker.moveToThread(self._cluster_thread)
        self._clustering_requested.connect(self._cluster_worker.build_clusters)
        self._cluster_worker.finished.connect(self._handle_clustering_finished)
        self._cluster_thread.finished.connect(self._cluster_worker.deleteLater)
        self._cluster_thread.start()
        self._cluster_request_id = 0

    def set_assets(self, assets: Iterable[GeotaggedAsset], library_root: Path) -> None:
        """Replace the asset catalogue shown on the map."""

        self._assets = [asset for asset in assets if isinstance(asset, GeotaggedAsset)]
        self._library_root = library_root
        self._city_annotations = []
        self._thumbnail_loader.reset_for_album(library_root)
        self.thumbnailsInvalidated.emit()
        self.citiesUpdated.emit([])
        self._schedule_cluster_update()

    def clear(self) -> None:
        """Remove all markers and cancel outstanding work."""

        self._cluster_worker.interrupt()
        self._cluster_request_id += 1
        self._assets = []
        self._clusters = []
        self._city_annotations = []
        self._library_root = None
        self._is_panning = False
        self._view_center_x = 0.5
        self._view_center_y = 0.5
        self._view_zoom = float(self._map_widget.zoom)
        self._cluster_timer.stop()
        self.clustersUpdated.emit([])
        self.citiesUpdated.emit([])
        self.thumbnailsInvalidated.emit()

    def shutdown(self) -> None:
        """Stop worker threads so the application can exit cleanly."""

        self._cluster_worker.interrupt()
        if self._cluster_thread.isRunning():
            self._cluster_thread.quit()
            self._cluster_thread.wait()

    def handle_view_changed(self, center_x: float, center_y: float, zoom: float) -> None:
        """Record the latest viewport parameters and rebuild clusters lazily."""

        self._view_center_x = float(center_x)
        self._view_center_y = float(center_y)
        self._view_zoom = float(zoom)
        self._schedule_cluster_update()

    def handle_pan(self, delta: QPointF) -> None:
        """Shift visible markers while the user drags the map."""

        self._is_panning = True
        if self._cluster_timer.isActive():
            self._cluster_timer.stop()

        if not self._clusters:
            self.clustersUpdated.emit([])
            return

        for marker in self._clusters:
            marker.screen_pos = QPointF(
                marker.screen_pos.x() + delta.x(),
                marker.screen_pos.y() + delta.y(),
            )
            if marker.bounding_rect:
                marker.bounding_rect.translate(delta.x(), delta.y())

        self.clustersUpdated.emit(self._clusters)

    def handle_pan_finished(self) -> None:
        """Resume background clustering once the drag gesture ends."""

        self._is_panning = False
        self._schedule_cluster_update()

    def handle_resize(self) -> None:
        """React to widget size changes by recomputing clusters."""

        self._schedule_cluster_update()

    def handle_marker_click(self, cluster: _MarkerCluster) -> None:
        """Process a marker click by zooming or activating an asset."""

        if len(cluster.assets) == 1:
            asset = cluster.representative
            self.assetActivated.emit(asset.library_relative)
        else:
            self._map_widget.focus_on(cluster.longitude, cluster.latitude, zoom_delta=0.8)

    def handle_thumbnail_ready(self, root: Path, rel: str, pixmap: QPixmap) -> None:
        """Forward freshly rendered thumbnails to the UI layer."""

        if self._library_root is None or root != self._library_root:
            return
        if pixmap.isNull():
            return
        self.thumbnailUpdated.emit(rel, pixmap)

    def cluster_at(self, position: QPointF) -> Optional[_MarkerCluster]:
        """Return the foremost cluster that intersects *position*."""

        for cluster in reversed(self._clusters):
            if cluster.bounding_rect.contains(position):
                return cluster
        return None

    def _schedule_cluster_update(self) -> None:
        if self._is_panning:
            return
        self._cluster_timer.start()

    def _rebuild_clusters(self) -> None:
        if not self._assets:
            self._cluster_worker.interrupt()
            self._cluster_request_id += 1
            self._clusters = []
            if self._city_annotations:
                self._city_annotations = []
                self.citiesUpdated.emit([])
            self.clustersUpdated.emit([])
            return

        fetch_level = max(0, int(math.floor(self._view_zoom)))
        if fetch_level < self.CITY_LABEL_FETCH_LEVEL:
            # Ensure city labels disappear immediately when the user zooms out
            # so the map does not momentarily display annotations that no
            # longer correspond to any visible thumbnail clusters.
            self._update_city_annotations_for_clusters([])
        self._rebuild_photo_clusters()

    def _update_city_annotations_for_clusters(
        self, clusters: Sequence[_MarkerCluster]
    ) -> None:
        """Publish city labels that correspond to the currently visible clusters."""

        fetch_level = max(0, int(math.floor(self._view_zoom)))
        if fetch_level < self.CITY_LABEL_FETCH_LEVEL or not clusters:
            if self._city_annotations:
                # Clearing the cache prevents the renderer from drawing labels
                # that are no longer associated with a thumbnail cluster.
                self._city_annotations = []
                self.citiesUpdated.emit([])
            return

        annotations: list[CityAnnotation] = []
        for cluster in clusters:
            label = self._cluster_label(cluster)
            if label is None:
                continue
            name, display_name, tooltip = label
            matched_assets: list[GeotaggedAsset] = []
            for asset in cluster.assets:
                if not asset.location_name:
                    continue
                if self._normalise_location(asset.location_name) != name:
                    continue
                matched_assets.append(asset)
            if matched_assets:
                avg_lat = sum(item.latitude for item in matched_assets) / len(matched_assets)
                avg_lon = sum(item.longitude for item in matched_assets) / len(matched_assets)
            else:
                # Fall back to the geometric centre so the label remains near
                # the callout even when metadata differs between assets.
                avg_lat = cluster.latitude
                avg_lon = cluster.longitude
            annotations.append(
                CityAnnotation(
                    longitude=avg_lon,
                    latitude=avg_lat,
                    display_name=display_name,
                    full_name=tooltip,
                )
            )

        annotations.sort(key=lambda item: (item.display_name, item.full_name))
        if annotations != self._city_annotations:
            # Emit a copy so the renderer can keep a stable snapshot for hit
            # testing without being affected by subsequent mutations.
            self._city_annotations = annotations
            self.citiesUpdated.emit(list(self._city_annotations))

    def _format_city_name(self, raw_name: str) -> tuple[str, str]:
        """Return the display and tooltip strings for a raw location label."""

        normalized = " ".join(raw_name.split())
        if not normalized:
            return "", ""

        parts = normalized.split()
        primary = parts[0]
        remainder = normalized[len(primary) :].strip()
        remainder = remainder.lstrip(",;-/—–").strip()
        if remainder:
            tooltip = f"{primary} — {remainder}"
        else:
            tooltip = primary
        return primary, tooltip

    def _rebuild_photo_clusters(self) -> None:
        """Generate thumbnail clusters for the current viewport dimensions."""

        size = self._map_widget.size()
        if size.isEmpty():
            if self._clusters:
                self._clusters = []
                self.clustersUpdated.emit([])
            return

        threshold = max(self._marker_size * 0.6, 48.0)
        cell_size = max(int(threshold), 1)
        width = size.width()
        height = size.height()
        margin = self._marker_size

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

    def _handle_clustering_finished(
        self, request_id: int, clusters: list[_MarkerCluster]
    ) -> None:
        """Receive freshly computed clusters from the worker thread."""

        if request_id != self._cluster_request_id:
            return

        for cluster in clusters:
            cluster.bounding_rect = self._marker_rect(cluster.screen_pos)
            self._ensure_thumbnail(cluster.representative)

        self._clusters = clusters
        self._update_city_annotations_for_clusters(self._clusters)
        self.clustersUpdated.emit(self._clusters)

    def _cluster_label(
        self, cluster: _MarkerCluster
    ) -> Optional[tuple[str, str, str]]:
        """Return the normalised, display and tooltip text for *cluster*.

        The helper inspects the assets contained within *cluster* and selects
        the first non-empty location string. The name is normalised so that
        downstream comparisons ignore extra whitespace or punctuation.
        """

        for asset in cluster.assets:
            if not asset.location_name:
                continue
            normalised = self._normalise_location(asset.location_name)
            if not normalised:
                continue
            display_name, tooltip = self._format_city_name(normalised)
            if not display_name:
                continue
            return normalised, display_name, tooltip
        return None

    @staticmethod
    def _normalise_location(raw_name: str) -> str:
        """Coalesce whitespace and trim punctuation from *raw_name*.

        Normalisation allows the controller to match clusters even when the raw
        metadata varies between assets due to trailing whitespace or other
        accidental formatting differences.
        """

        return " ".join(raw_name.split()).strip()

    def _marker_rect(self, center: QPointF) -> QRectF:
        """Return the bounding box that mirrors the overlay's callout geometry."""

        height = float(self._marker_size)
        width = height
        x = center.x() - width / 2.0
        y = center.y() - height
        return QRectF(x, y, width, height)

    def _ensure_thumbnail(self, asset: GeotaggedAsset) -> None:
        if self._library_root is None:
            return
        size = QSize(self._thumbnail_size, self._thumbnail_size)
        pixmap = self._thumbnail_loader.request(
            asset.library_relative,
            asset.absolute_path,
            size,
            is_image=asset.is_image,
            is_video=asset.is_video,
            still_image_time=asset.still_image_time,
            duration=asset.duration,
        )
        if pixmap is not None and not pixmap.isNull():
            self.thumbnailUpdated.emit(asset.library_relative, pixmap)

    @staticmethod
    def distance(a: QPointF, b: QPointF) -> float:
        """Return the Euclidean distance between two screen positions."""

        return math.hypot(a.x() - b.x(), a.y() - b.y())

__all__ = ["MarkerController", "_MarkerCluster"]
