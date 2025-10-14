"""Utility helpers for manipulating geographic geometry data."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any, Tuple


def sequence_depth(value: object) -> int:
    """Return how many list/tuple levels ``value`` contains before scalars."""

    depth = 0
    current = value
    while isinstance(current, (list, tuple)) and current:
        depth += 1
        current = current[0]
    return depth


def normalize_geometry_type(raw_type: object) -> str | None:
    """Translate geometry identifiers into canonical GeoJSON-style strings."""

    if isinstance(raw_type, str):
        return raw_type
    if raw_type == 1:
        return "Point"
    if raw_type == 2:
        return "LineString"
    if raw_type == 3:
        return "Polygon"
    return None


def is_number_pair(value: Sequence[object]) -> bool:
    """Return ``True`` when ``value`` looks like an ``(x, y)`` tuple."""

    if len(value) < 2:
        return False
    return all(isinstance(component, (int, float)) for component in value[:2])


def map_coordinate_structure(
    value: object,
    transform: Callable[[float, float], tuple[float, float]],
) -> object:
    """Apply ``transform`` to every coordinate pair in ``value``."""

    if isinstance(value, (list, tuple)):
        if is_number_pair(value):
            x, y = transform(float(value[0]), float(value[1]))
            return (x, y)
        return [map_coordinate_structure(item, transform) for item in value]
    return value


def lonlat_to_tile_units(
    lon: float,
    lat: float,
    extent: int,
    tile_x: int,
    tile_y: int,
    fetch_zoom: int,
) -> tuple[float, float]:
    """Project longitude/latitude into tile-relative coordinates."""

    n = 1 << fetch_zoom
    lon = float(lon)
    lat = max(min(float(lat), 85.05112878), -85.05112878)

    world_x = (lon + 180.0) / 360.0 * n * extent
    sin_lat = math.sin(math.radians(lat))
    world_y = (0.5 + math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * n * extent

    tile_origin_x = tile_x * extent
    tile_origin_y = tile_y * extent
    return world_x - tile_origin_x, world_y - tile_origin_y


def convert_geojson_coordinates(
    geom_type: str | None,
    coordinates: object,
    extent: int,
    tile_x: int,
    tile_y: int,
    fetch_zoom: int,
) -> object:
    """Convert GeoJSON lon/lat coordinates to tile-relative coordinates."""

    if geom_type is None:
        return coordinates

    def transformer(lon: float, lat: float) -> tuple[float, float]:
        return lonlat_to_tile_units(lon, lat, extent, tile_x, tile_y, fetch_zoom)

    return map_coordinate_structure(coordinates, transformer)


def normalize_polygons(geom_type: str | None, coordinates: object) -> list[Sequence[Sequence[Tuple[float, float]]]]:
    """Convert raw polygon coordinates into a list of polygons."""

    polygons: list[Sequence[Sequence[Tuple[float, float]]]] = []
    if geom_type == "Polygon":
        polygons = [coordinates] if isinstance(coordinates, (list, tuple)) else []
    elif geom_type == "MultiPolygon":
        polygons = list(coordinates) if isinstance(coordinates, (list, tuple)) else []
    else:
        depth = sequence_depth(coordinates)
        if depth == 3:
            polygons = [coordinates] if isinstance(coordinates, (list, tuple)) else []
        elif depth >= 4:
            polygons = list(coordinates) if isinstance(coordinates, (list, tuple)) else []

    return [polygon for polygon in polygons if polygon]


def normalize_lines(geom_type: str | None, coordinates: object) -> list[Sequence[Tuple[float, float]]]:
    """Convert raw line coordinates into a list of line strings."""

    lines: list[Sequence[Tuple[float, float]]] = []
    if geom_type == "LineString":
        lines = [coordinates] if isinstance(coordinates, (list, tuple)) else []
    elif geom_type == "MultiLineString":
        lines = list(coordinates) if isinstance(coordinates, (list, tuple)) else []
    else:
        depth = sequence_depth(coordinates)
        if depth == 2:
            lines = [coordinates] if isinstance(coordinates, (list, tuple)) else []
        elif depth >= 3:
            lines = list(coordinates) if isinstance(coordinates, (list, tuple)) else []

    return [line for line in lines if line]


def normalize_points(geom_type: str | None, coordinates: object) -> list[Tuple[float, float]]:
    """Convert raw point coordinates into a list of ``(x, y)`` tuples."""

    points: list[Tuple[float, float]] = []
    if geom_type == "Point":
        if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
            points = [tuple(coordinates[:2])]  # type: ignore[arg-type]
    elif geom_type == "MultiPoint":
        if isinstance(coordinates, (list, tuple)):
            points = [
                tuple(point[:2])
                for point in coordinates
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
    else:
        depth = sequence_depth(coordinates)
        if depth == 1 and isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
            points = [tuple(coordinates[:2])]  # type: ignore[arg-type]
        elif depth >= 2 and isinstance(coordinates, (list, tuple)):
            points = [
                tuple(point[:2])
                for point in coordinates
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]

    return points


def extract_geometry(
    feature: dict,
    extent: int,
    tile_x: int,
    tile_y: int,
    is_lonlat: bool,
    fetch_zoom: int,
) -> tuple[str | None, object]:
    """Return geometry information normalised to the tile coordinate space."""

    geometry = feature.get("geometry")
    if isinstance(geometry, dict):
        geom_type = geometry.get("type")
        coordinates = geometry.get("coordinates", [])
    else:
        geom_type = feature.get("type")
        coordinates = geometry

    normalized_type = normalize_geometry_type(geom_type)
    if is_lonlat:
        normalized_coordinates = convert_geojson_coordinates(
            normalized_type,
            coordinates,
            extent,
            tile_x,
            tile_y,
            fetch_zoom,
        )
    else:
        normalized_coordinates = coordinates

    return normalized_type, normalized_coordinates


__all__ = [
    "convert_geojson_coordinates",
    "extract_geometry",
    "is_number_pair",
    "lonlat_to_tile_units",
    "map_coordinate_structure",
    "normalize_geometry_type",
    "normalize_lines",
    "normalize_points",
    "normalize_polygons",
    "sequence_depth",
]
