"""Rendering logic for :class:`~map_widget.map_widget.MapWidget`."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from iPhotos.maps.style_resolver import StyleResolver

from .geometry import extract_geometry, normalize_lines, normalize_points, normalize_polygons
from .layer import LayerPlan
from .tile_manager import TileManager


# ``MAX_TILE_ZOOM_LEVEL`` reflects the highest tile zoom level available from the
# bundled map source. When the view zoom exceeds this value we keep rendering the
# level-6 tiles and upscale them so that the user never sees empty space.
MAX_TILE_ZOOM_LEVEL = 6
MERCATOR_LAT_BOUND = 85.05112878


@dataclass(frozen=True)
class _ViewState:
    """Describe the camera parameters used for the current paint pass."""

    zoom: float
    fetch_zoom: int
    width: int
    height: int
    view_top_left_x: float
    view_top_left_y: float
    scaled_tile_size: float
    tiles_across: int


@dataclass(frozen=True)
class CityAnnotation:
    """Descriptor describing a lightweight label drawn directly on the map."""

    longitude: float
    latitude: float
    display_name: str
    full_name: str


@dataclass
class _RenderedCityLabel:
    """Runtime data cached for hit-testing rendered city annotations."""

    bounds: QRectF
    full_name: str


class MapRenderer:
    """Render MapLibre vector tiles using the provided tile manager."""

    CITY_LABEL_MIN_FETCH_LEVEL = 5

    def __init__(
        self,
        *,
        style: StyleResolver,
        tile_manager: TileManager,
        layers: Sequence[LayerPlan],
        tile_size: int,
    ) -> None:
        self._style = style
        self._tile_manager = tile_manager
        self._layers = list(layers)
        self._tile_size = tile_size
        self._path_cache: Dict[tuple, QPainterPath] = {}
        # ``_cities`` holds the source annotations supplied by the UI layer while
        # ``_city_labels`` caches the screen-space bounds calculated during the
        # most recent render pass.  The cached rectangles allow the widget to
        # answer hover tooltips without re-running the projection math on every
        # mouse move event.
        self._cities: list[CityAnnotation] = []
        self._city_labels: list[_RenderedCityLabel] = []

    # ------------------------------------------------------------------
    def set_cities(self, cities: Iterable[CityAnnotation]) -> None:
        """Replace the annotations drawn directly on top of the map tiles."""

        self._cities = [
            CityAnnotation(city.longitude, city.latitude, city.display_name, city.full_name)
            for city in cities
        ]
        self._city_labels.clear()

    # ------------------------------------------------------------------
    def render(
        self,
        painter: QPainter,
        *,
        center_x: float,
        center_y: float,
        zoom: float,
        width: int,
        height: int,
    ) -> None:
        """Draw the current map scene into ``painter``."""

        # ``fillRect`` clears the previous frame and establishes the water-colored
        # background used across the application.
        painter.fillRect(0, 0, width, height, QColor("#88a8c2"))

        view_state = self._compute_view_state(center_x, center_y, zoom, width, height)
        tiles_to_draw, tiles_to_request = self._collect_tiles(view_state)
        self._request_tiles(tiles_to_request)
        self._render_tiles(painter, tiles_to_draw, view_state)
        self._render_cities(painter, view_state)

    # ------------------------------------------------------------------
    def invalidate_tile(self, tile_key: tuple[int, int, int]) -> None:
        """Remove cached geometry derived from ``tile_key``."""

        self._clear_tile_paths(tile_key)

    # ------------------------------------------------------------------
    def city_at(self, position: QPointF) -> Optional[str]:
        """Return the full city name for the label intersecting ``position``."""

        for label in reversed(self._city_labels):
            if label.bounds.contains(position):
                return label.full_name
        return None

    # ------------------------------------------------------------------
    def _compute_view_state(
        self,
        center_x: float,
        center_y: float,
        zoom: float,
        width: int,
        height: int,
    ) -> _ViewState:
        """Translate widget geometry into the parameters used during rendering."""

        world_size = self._tile_size * (2 ** zoom)
        center_px = center_x * world_size
        center_py = center_y * world_size
        view_top_left_x = center_px - width / 2.0
        view_top_left_y = center_py - height / 2.0

        # Clamp the requested tile zoom level to the available range so that
        # the renderer keeps drawing level-6 tiles when the interactive zoom is
        # higher than the tile set supports. The resulting ``scale_factor``
        # ensures that those tiles are magnified to match the desired view.
        fetch_zoom = min(MAX_TILE_ZOOM_LEVEL, max(0, math.floor(zoom)))
        tiles_across = 1 << fetch_zoom if fetch_zoom >= 0 else 1
        scale_factor = 2 ** (zoom - fetch_zoom)
        scaled_tile_size = self._tile_size * scale_factor

        return _ViewState(
            zoom=zoom,
            fetch_zoom=fetch_zoom,
            width=width,
            height=height,
            view_top_left_x=view_top_left_x,
            view_top_left_y=view_top_left_y,
            scaled_tile_size=scaled_tile_size,
            tiles_across=tiles_across,
        )

    # ------------------------------------------------------------------
    def _collect_tiles(
        self, view_state: _ViewState
    ) -> tuple[list[tuple[tuple[int, int, int], dict, float, float, int, int]], list[tuple[float, tuple[int, int, int]]]]:
        """Gather tiles that intersect the viewport and schedule missing ones."""

        start_tile_x = math.floor(view_state.view_top_left_x / view_state.scaled_tile_size)
        start_tile_y = math.floor(view_state.view_top_left_y / view_state.scaled_tile_size)
        end_tile_x = math.ceil(
            (view_state.view_top_left_x + view_state.width) / view_state.scaled_tile_size
        )
        end_tile_y = math.ceil(
            (view_state.view_top_left_y + view_state.height) / view_state.scaled_tile_size
        )

        tiles_to_draw: list[tuple[tuple[int, int, int], dict, float, float, int, int]] = []
        tiles_to_request: list[tuple[float, tuple[int, int, int]]] = []

        for tile_y in range(start_tile_y, end_tile_y):
            if tile_y < 0 or tile_y >= view_state.tiles_across:
                continue
            for tile_x in range(start_tile_x, end_tile_x):
                wrapped_x = tile_x % view_state.tiles_across
                flipped_y = (view_state.tiles_across - 1) - tile_y
                tile_key = (view_state.fetch_zoom, wrapped_x, flipped_y)

                tile_origin_x = tile_x * view_state.scaled_tile_size - view_state.view_top_left_x
                tile_origin_y = tile_y * view_state.scaled_tile_size - view_state.view_top_left_y

                tile_data = self._tile_manager.get_tile(tile_key)
                if tile_data is None:
                    if not self._tile_manager.is_tile_missing(tile_key):
                        tile_center_x = tile_origin_x + view_state.scaled_tile_size / 2.0
                        tile_center_y = tile_origin_y + view_state.scaled_tile_size / 2.0
                        dist_sq = (
                            (tile_center_x - view_state.width / 2.0) ** 2
                            + (tile_center_y - view_state.height / 2.0) ** 2
                        )
                        tiles_to_request.append((dist_sq, tile_key))
                    continue

                tiles_to_draw.append(
                    (tile_key, tile_data, tile_origin_x, tile_origin_y, wrapped_x, tile_y)
                )

        return tiles_to_draw, tiles_to_request

    # ------------------------------------------------------------------
    def _request_tiles(self, tiles_to_request: list[tuple[float, tuple[int, int, int]]]) -> None:
        """Submit background load requests for tiles sorted by distance."""

        if not tiles_to_request:
            return

        tiles_to_request.sort(key=lambda item: item[0])
        for _, tile_key in tiles_to_request:
            self._tile_manager.ensure_tile(tile_key)

    # ------------------------------------------------------------------
    def _render_tiles(
        self,
        painter: QPainter,
        tiles_to_draw: list[tuple[tuple[int, int, int], dict, float, float, int, int]],
        view_state: _ViewState,
    ) -> None:
        """Iterate over the visible tiles and draw each requested layer."""

        for tile_key, tile_data, tile_origin_x, tile_origin_y, wrapped_x, tile_y in tiles_to_draw:
            for plan in self._layers:
                if not self._style.is_layer_visible(plan.style_layer, view_state.zoom):
                    continue
                layer = tile_data.get(plan.source_layer)
                if not layer:
                    continue
                extent = layer.get("extent", 4096)
                features = layer.get("features", [])
                if not features or not extent:
                    continue

                scale = view_state.scaled_tile_size / float(extent)

                if plan.kind in {"fill", "line"}:
                    painter.save()
                    transform = painter.transform()
                    transform.translate(tile_origin_x, tile_origin_y)
                    transform.scale(scale, scale)
                    painter.setTransform(transform)

                    if plan.kind == "fill":
                        self._draw_polygons(
                            painter,
                            tile_key,
                            features,
                            plan.style_layer,
                            extent,
                            wrapped_x,
                            tile_y,
                            plan.is_lonlat,
                            view_state.zoom,
                            view_state.fetch_zoom,
                        )
                    else:
                        self._draw_lines(
                            painter,
                            tile_key,
                            features,
                            plan.style_layer,
                            extent,
                            wrapped_x,
                            tile_y,
                            plan.is_lonlat,
                            view_state.zoom,
                            view_state.fetch_zoom,
                        )
                    painter.restore()
                elif plan.kind == "symbol":
                    self._draw_symbols(
                        painter,
                        features,
                        plan.style_layer,
                        extent,
                        tile_origin_x,
                        tile_origin_y,
                        view_state.scaled_tile_size,
                        wrapped_x,
                        tile_y,
                        plan.is_lonlat,
                        view_state.zoom,
                        view_state.fetch_zoom,
                    )

    # ------------------------------------------------------------------
    def _render_cities(self, painter: QPainter, view_state: _ViewState) -> None:
        """Render lightweight city labels supplied by the surrounding UI."""

        self._city_labels.clear()
        if not self._cities:
            return
        if view_state.fetch_zoom < self.CITY_LABEL_MIN_FETCH_LEVEL:
            return

        world_size = float(self._tile_size * (2 ** view_state.zoom))
        if world_size <= 0.0:
            return

        center_px = view_state.view_top_left_x + view_state.width / 2.0
        half_world = world_size / 2.0

        font = QFont("Open Sans", pointSize=11)
        font.setBold(True)
        metrics = QFontMetricsF(font)

        dot_radius = 4.0
        text_gap = 6.0
        halo_pen = QPen(QColor(255, 255, 255, 220))
        halo_pen.setWidthF(3.0)
        halo_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        halo_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        halo_pen.setCosmetic(True)
        outline_pen = QPen(QColor(255, 255, 255, 220))
        outline_pen.setWidthF(2.0)
        outline_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        outline_pen.setCosmetic(True)
        label_pen = QPen(QColor("#2b2b2b"))
        label_pen.setCosmetic(True)

        painter.save()
        painter.setFont(font)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        rendered_label_boxes: list[QRectF] = []
        for city in self._cities:
            if not city.display_name:
                continue

            world_position = self._lonlat_to_world(city.longitude, city.latitude, world_size)
            if world_position is None:
                continue
            world_x, world_y = world_position

            delta_x = world_x - center_px
            if delta_x > half_world:
                world_x -= world_size
            elif delta_x < -half_world:
                world_x += world_size

            screen_x = world_x - view_state.view_top_left_x
            screen_y = world_y - view_state.view_top_left_y

            margin = 32.0
            if (
                screen_x < -margin
                or screen_y < -margin
                or screen_x > view_state.width + margin
                or screen_y > view_state.height + margin
            ):
                continue

            text_rect = metrics.boundingRect(city.display_name)
            text_width = float(text_rect.width())
            text_height = float(text_rect.height())
            baseline_y = screen_y + text_height / 2.0 - metrics.descent()
            text_top = baseline_y - metrics.ascent()
            text_left = screen_x + dot_radius + text_gap

            text_path = QPainterPath()
            text_path.addText(QPointF(text_left, baseline_y), font, city.display_name)

            painter.setPen(halo_pen)
            painter.drawPath(text_path)

            painter.setPen(outline_pen)
            painter.setBrush(QColor("#1e73ff"))
            painter.drawEllipse(QPointF(screen_x, screen_y), dot_radius, dot_radius)

            painter.setPen(label_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawText(QPointF(text_left, baseline_y), city.display_name)

            bounds_left = min(screen_x - dot_radius, text_left)
            bounds_top = min(screen_y - dot_radius, text_top)
            bounds_right = max(screen_x + dot_radius, text_left + text_width)
            bounds_bottom = max(screen_y + dot_radius, text_top + text_height)
            bounds = QRectF(
                bounds_left - 2.0,
                bounds_top - 2.0,
                (bounds_right - bounds_left) + 4.0,
                (bounds_bottom - bounds_top) + 4.0,
            )
            # Skip labels whose padded bounding box would overlap an annotation
            # that has already been drawn. The conservative buffer keeps the map
            # readable when multiple cities are clustered together.
            if any(bounds.intersects(existing_box) for existing_box in rendered_label_boxes):
                continue
            rendered_label_boxes.append(bounds)
            full_name = city.full_name or city.display_name
            self._city_labels.append(_RenderedCityLabel(bounds=bounds, full_name=full_name))

        painter.restore()

    # ------------------------------------------------------------------
    def _make_path_cache_key(
        self,
        tile_key: tuple[int, int, int],
        *,
        plan_kind: str,
        style_layer: str,
        feature_index: int,
        extra: object | None = None,
    ) -> tuple:
        """Build a stable key for cached geometry associated with ``tile_key``."""

        if extra is None:
            return (plan_kind, *tile_key, style_layer, feature_index)
        return (plan_kind, *tile_key, style_layer, feature_index, extra)

    # ------------------------------------------------------------------
    def _clear_tile_paths(self, tile_key: tuple[int, int, int]) -> None:
        """Remove any cached paths associated with ``tile_key``."""

        keys_to_remove = [key for key in self._path_cache if key[1:4] == tile_key]
        for key in keys_to_remove:
            del self._path_cache[key]

    # ------------------------------------------------------------------
    def _draw_polygons(
        self,
        painter: QPainter,
        tile_key: tuple[int, int, int],
        features: Sequence[dict],
        style_layer: str,
        extent: int,
        tile_x: int,
        tile_y: int,
        is_lonlat: bool,
        zoom: float,
        fetch_zoom: int,
    ) -> None:
        for feature_index, feature in enumerate(features):
            geom_type, coordinates = extract_geometry(
                feature,
                extent,
                tile_x,
                tile_y,
                is_lonlat,
                fetch_zoom,
            )
            polygons = normalize_polygons(geom_type, coordinates)
            if not polygons:
                continue

            properties = feature.get("properties", {})
            if not self._style.feature_matches_filter(style_layer, properties):
                continue

            brush, outline_pen = self._style.resolve_fill_style(style_layer, zoom, properties)
            if brush.style() == Qt.NoBrush and outline_pen is None:
                continue

            cache_key = self._make_path_cache_key(
                tile_key,
                plan_kind="fill",
                style_layer=style_layer,
                feature_index=feature_index,
            )
            path = self._path_cache.get(cache_key)
            if path is None:
                path = QPainterPath()
                path.setFillRule(Qt.OddEvenFill)
                for polygon in polygons:
                    self._append_polygon(path, polygon, extent)
                self._path_cache[cache_key] = path

            painter.save()
            painter.setBrush(brush)
            if outline_pen is not None:
                pen = QPen(outline_pen)
                pen.setCosmetic(True)
                painter.setPen(pen)
            else:
                painter.setPen(Qt.NoPen)
            painter.drawPath(path)
            painter.restore()

    # ------------------------------------------------------------------
    def _draw_lines(
        self,
        painter: QPainter,
        tile_key: tuple[int, int, int],
        features: Sequence[dict],
        style_layer: str,
        extent: int,
        tile_x: int,
        tile_y: int,
        is_lonlat: bool,
        zoom: float,
        fetch_zoom: int,
    ) -> None:
        for feature_index, feature in enumerate(features):
            geom_type, coordinates = extract_geometry(
                feature,
                extent,
                tile_x,
                tile_y,
                is_lonlat,
                fetch_zoom,
            )
            if geom_type is None:
                continue

            properties = feature.get("properties", {})
            if not self._style.feature_matches_filter(style_layer, properties):
                continue

            pen_style = self._style.resolve_line_style(style_layer, zoom, properties)
            if pen_style is None:
                continue

            cache_key = self._make_path_cache_key(
                tile_key,
                plan_kind="line",
                style_layer=style_layer,
                feature_index=feature_index,
                extra=geom_type,
            )
            path = self._path_cache.get(cache_key)
            if path is None:
                path = QPainterPath()
                if geom_type in {"LineString", "MultiLineString"}:
                    lines = normalize_lines(geom_type, coordinates)
                    for line in lines:
                        self._append_line(path, line, extent)
                else:
                    polygons = normalize_polygons(geom_type, coordinates)
                    for polygon in polygons:
                        self._append_polygon(path, polygon, extent)
                self._path_cache[cache_key] = path

            painter.save()
            pen = QPen(pen_style)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            painter.restore()

    # ------------------------------------------------------------------
    def _draw_symbols(
        self,
        painter: QPainter,
        features: Sequence[dict],
        style_layer: str,
        extent: int,
        origin_x: float,
        origin_y: float,
        scaled_tile_size: float,
        tile_x: int,
        tile_y: int,
        is_lonlat: bool,
        zoom: float,
        fetch_zoom: int,
    ) -> None:
        if extent <= 0:
            return

        scale = scaled_tile_size / float(extent)
        for feature in features:
            geom_type, coordinates = extract_geometry(
                feature,
                extent,
                tile_x,
                tile_y,
                is_lonlat,
                fetch_zoom,
            )

            properties = feature.get("properties", {})
            if not self._style.feature_matches_filter(style_layer, properties):
                continue

            placement = self._style.get_layout(style_layer, "symbol-placement", zoom, properties)
            points = self._resolve_symbol_points(
                geom_type,
                coordinates,
                placement,
            )
            if not points:
                continue

            text_style = self._style.resolve_text_style(style_layer, zoom, properties)
            if text_style is None or not text_style.text:
                continue

            painter.save()
            font = QFont("Open Sans", pointSize=10)
            font.setPointSizeF(text_style.size)
            painter.setFont(font)

            metrics = QFontMetricsF(font)
            rect = metrics.boundingRect(text_style.text)
            half_width = rect.width() / 2.0
            baseline_offset = rect.height() / 2.0 - metrics.descent()

            for point in points:
                tile_x_unit = float(point[0])
                tile_y_unit = float(point[1])

                screen_center_x = origin_x + scale * tile_x_unit
                screen_center_y = origin_y + scale * (extent - tile_y_unit)

                baseline_x = screen_center_x - half_width
                baseline_y = screen_center_y + baseline_offset

                if text_style.halo_color and text_style.halo_width > 0:
                    halo_path = QPainterPath()
                    halo_path.addText(QPointF(baseline_x, baseline_y), font, text_style.text)
                    halo_pen = QPen(text_style.halo_color, text_style.halo_width)
                    halo_pen.setJoinStyle(Qt.RoundJoin)
                    halo_pen.setCosmetic(True)
                    painter.setPen(halo_pen)
                    painter.setBrush(Qt.NoBrush)
                    painter.drawPath(halo_path)

                label_pen = QPen(text_style.color)
                label_pen.setCosmetic(True)
                painter.setPen(label_pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawText(QPointF(baseline_x, baseline_y), text_style.text)

            painter.restore()

    # ------------------------------------------------------------------
    def _resolve_symbol_points(
        self,
        geom_type: str | None,
        coordinates: object,
        placement: object,
    ) -> list[Tuple[float, float]]:
        """Return representative anchor points for symbol placement.

        MapLibre styles can request that labels follow lines (``symbol-placement``
        set to ``"line"``).  The preview renderer does not support full text
        shaping along a path, so instead we place a single label near the center
        of each feature.  This keeps the output legible without rendering the
        same text dozens of times along a meridian or parallel.
        """

        placement_value = str(placement).lower() if isinstance(placement, str) else ""
        if placement_value == "line":
            anchors: list[Tuple[float, float]] = []

            # Line geometries receive a midpoint anchor that approximates the
            # behaviour of a proper path label while avoiding heavy text
            # duplication.
            if geom_type in {"LineString", "MultiLineString"}:
                for line in normalize_lines(geom_type, coordinates):
                    anchor = self._line_midpoint(line)
                    if anchor is not None:
                        anchors.append(anchor)

            # Polygon geometries occasionally share the same "line"
            # placement hint (for example graticule backgrounds).  Using the
            # outer ring ensures the label lands near the visual outline.
            elif geom_type in {"Polygon", "MultiPolygon"}:
                for polygon in normalize_polygons(geom_type, coordinates):
                    if not polygon:
                        continue
                    anchor = self._line_midpoint(polygon[0])
                    if anchor is not None:
                        anchors.append(anchor)

            if anchors:
                return anchors

        points = normalize_points(geom_type, coordinates)
        if not points:
            return []

        if placement_value == "line" and len(points) > 1:
            # As a final safeguard collapse dense point collections into a
            # single representative anchor so that fallback logic never spams
            # identical labels across the same line feature.
            middle_index = len(points) // 2
            return [points[middle_index]]

        return points

    # ------------------------------------------------------------------
    @staticmethod
    def _line_midpoint(line: Sequence[Tuple[float, float]]) -> Tuple[float, float] | None:
        """Return the coordinate located halfway along ``line``.

        The helper walks the polyline until half the total length is reached,
        interpolating the exact anchor location on the current segment.  A
        ``None`` result indicates that the polyline did not provide enough
        distinct coordinates to compute a stable midpoint.
        """

        if not line:
            return None
        if len(line) == 1:
            return float(line[0][0]), float(line[0][1])

        total_length = 0.0
        segment_lengths: list[float] = []
        for index in range(1, len(line)):
            start = line[index - 1]
            end = line[index]
            length = math.hypot(float(end[0]) - float(start[0]), float(end[1]) - float(start[1]))
            segment_lengths.append(length)
            total_length += length

        if total_length <= 0.0:
            return None

        halfway = total_length / 2.0
        distance_accumulated = 0.0
        for index, length in enumerate(segment_lengths, start=1):
            if distance_accumulated + length >= halfway:
                start = line[index - 1]
                end = line[index]
                if length <= 0.0:
                    return float(start[0]), float(start[1])
                ratio = (halfway - distance_accumulated) / length
                anchor_x = float(start[0]) + (float(end[0]) - float(start[0])) * ratio
                anchor_y = float(start[1]) + (float(end[1]) - float(start[1])) * ratio
                return anchor_x, anchor_y
            distance_accumulated += length

        last_point = line[-1]
        return float(last_point[0]), float(last_point[1])

    # ------------------------------------------------------------------
    def _append_polygon(
        self,
        path: QPainterPath,
        polygon: Sequence[Sequence[Tuple[float, float]]],
        extent: int,
    ) -> None:
        """Add ``polygon`` rings to ``path`` within the tile coordinate space."""

        for ring in polygon:
            if len(ring) < 3:
                continue
            first_point = ring[0]
            path.moveTo(first_point[0], extent - first_point[1])
            for point in ring[1:]:
                path.lineTo(point[0], extent - point[1])
            path.closeSubpath()

    # ------------------------------------------------------------------
    def _append_line(
        self,
        path: QPainterPath,
        line: Sequence[Tuple[float, float]],
        extent: int,
    ) -> None:
        """Append a single line string to ``path`` in tile coordinates."""

        if len(line) < 2:
            return

        start = line[0]
        path.moveTo(start[0], extent - start[1])
        for point in line[1:]:
            path.lineTo(point[0], extent - point[1])

    # ------------------------------------------------------------------
    def _lonlat_to_world(
        self, lon: float, lat: float, world_size: float
    ) -> Optional[tuple[float, float]]:
        """Convert geographic coordinates to Mercator world coordinates."""

        try:
            lon_value = float(lon)
            lat_value = float(lat)
        except (TypeError, ValueError):
            return None

        lat_value = max(min(lat_value, MERCATOR_LAT_BOUND), -MERCATOR_LAT_BOUND)
        x = (lon_value + 180.0) / 360.0 * world_size
        sin_lat = math.sin(math.radians(lat_value))
        y = (
            0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)
        ) * world_size
        return x, y


__all__ = ["MapRenderer", "CityAnnotation"]
