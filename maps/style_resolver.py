"""Helpers for interpreting a subset of the MapLibre style specification.

The real specification contains a large collection of features.  For the
purpose of this desktop preview application we only need a relatively small
subset, namely enough to color polygons, draw lines and display simple labels.
This module focuses on that subset while keeping the API intentionally small
and easy to extend when additional style features become necessary.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPen

_COLOR_MATCHER = re.compile(r"rgba\(([^)]+)\)")
_LOGGER = logging.getLogger(__name__)


class StyleLoadError(Exception):
    """Raised when the style sheet cannot be read or parsed."""


@dataclass(frozen=True)
class TextStyle:
    """Convenience container describing how to render a text label."""

    text: str
    size: float
    color: QColor
    halo_color: Optional[QColor]
    halo_width: float


class StyleResolver:
    """Interpret style definitions stored in ``style.json``.

    Only the handful of layers required by the preview application are
    extracted.  Unknown layers are ignored which keeps the class friendly to
    style files that contain more information than we actually render.
    """

    def __init__(self, style_path: Path | str) -> None:
        self._style_path = Path(style_path)
        try:
            raw_data = self._style_path.read_text(encoding="utf8")
        except OSError as exc:
            raise StyleLoadError(f"Unable to read style file '{self._style_path}'") from exc

        try:
            self._style = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise StyleLoadError(f"Style file '{self._style_path}' is not valid JSON") from exc

        self._layers: Dict[str, Dict[str, Any]] = {
            layer["id"]: layer for layer in self._style.get("layers", []) if isinstance(layer.get("id"), str)
        }

    # ------------------------------------------------------------------
    def feature_matches_filter(self, layer_id: str, properties: Dict[str, Any]) -> bool:
        """Return ``True`` when the feature satisfies the layer filter.

        MapLibre styles can attach filter expressions to layers.  The preview
        renderer only needs a very small subset of the full expression
        language, namely the operators that are used by the bundled style
        sheet.  Evaluating the filter here keeps the drawing code in
        :mod:`map_widget` focused on geometry conversion and painting logic.
        """

        layer = self._layers.get(layer_id)
        if not layer:
            return False

        filter_expression = layer.get("filter")
        if not filter_expression:
            return True

        return self._evaluate_filter(filter_expression, properties)

    # ------------------------------------------------------------------
    def get_paint(self, layer_id: str, key: str, zoom: float, properties: Dict[str, Any]) -> Any:
        """Return the evaluated paint value for the given layer and key."""

        layer = self._layers.get(layer_id)
        if not layer:
            return None
        paint = layer.get("paint", {})
        value = paint.get(key)
        return self._evaluate(value, zoom, properties)

    # ------------------------------------------------------------------
    def vector_layer_definitions(self) -> list[Dict[str, Any]]:
        """Return metadata describing renderable vector tile layers."""

        definitions: list[Dict[str, Any]] = []
        for layer in self._style.get("layers", []):
            if not isinstance(layer, dict):
                continue
            kind = layer.get("type")
            if kind not in {"fill", "line", "symbol"}:
                continue
            source_layer = layer.get("source-layer")
            layer_id = layer.get("id")
            if not isinstance(source_layer, str) or not isinstance(layer_id, str):
                continue
            metadata = layer.get("metadata") or {}
            is_lonlat = bool(metadata.get("map-preview:is_lonlat", False))
            definitions.append(
                {
                    "style_layer": layer_id,
                    "source_layer": source_layer,
                    "kind": kind,
                    "is_lonlat": is_lonlat,
                }
            )
        return definitions

    # ------------------------------------------------------------------
    def get_layout(self, layer_id: str, key: str, zoom: float, properties: Dict[str, Any]) -> Any:
        """Return the evaluated layout value for the given layer and key."""

        layer = self._layers.get(layer_id)
        if not layer:
            return None
        layout = layer.get("layout", {})
        value = layout.get(key)
        return self._evaluate(value, zoom, properties)

    # ------------------------------------------------------------------
    def resolve_fill_style(self, layer_id: str, zoom: float, properties: Dict[str, Any]) -> tuple[QBrush, Optional[QPen]]:
        """Return a brush/pen combination for a ``fill`` layer.

        The helper applies opacity settings and fallbacks so the caller can
        simply use the returned brush and optional outline pen when rendering
        the polygons associated with the layer.
        """

        brush = QBrush(Qt.NoBrush)
        outline_pen: Optional[QPen] = None

        layer = self._layers.get(layer_id)
        if not layer or layer.get("type") != "fill":
            return brush, outline_pen

        color = self.get_paint(layer_id, "fill-color", zoom, properties)
        if isinstance(color, QColor):
            brush = QBrush(QColor(color))
        else:
            # Provide a friendly default so the preview remains readable even
            # when the style omits an explicit fill color.
            brush = QBrush(QColor("#eab38f"))

        opacity = self.get_paint(layer_id, "fill-opacity", zoom, properties)
        if isinstance(opacity, (int, float)):
            color_with_alpha = QColor(brush.color())
            color_with_alpha.setAlphaF(_clamp01(opacity))
            brush = QBrush(color_with_alpha)

        outline_color = self.get_paint(layer_id, "fill-outline-color", zoom, properties)
        if isinstance(outline_color, QColor):
            outline_pen = QPen(QColor(outline_color))
            outline_pen.setCosmetic(True)

        return brush, outline_pen

    # ------------------------------------------------------------------
    def resolve_line_style(self, layer_id: str, zoom: float, properties: Dict[str, Any]) -> Optional[QPen]:
        """Return a configured :class:`QPen` for a ``line`` layer."""

        layer = self._layers.get(layer_id)
        if not layer or layer.get("type") != "line":
            return None

        color = self.get_paint(layer_id, "line-color", zoom, properties)
        if not isinstance(color, QColor):
            color = QColor("#ffffff")

        width = self.get_paint(layer_id, "line-width", zoom, properties)
        if not isinstance(width, (int, float)):
            width = 1.0

        pen = QPen(QColor(color))
        pen.setCosmetic(True)
        pen.setWidthF(float(width))

        opacity = self.get_paint(layer_id, "line-opacity", zoom, properties)
        if isinstance(opacity, (int, float)):
            color_with_alpha = QColor(color)
            color_with_alpha.setAlphaF(_clamp01(opacity))
            pen.setColor(color_with_alpha)

        dash_array = self.get_paint(layer_id, "line-dasharray", zoom, properties)
        if isinstance(dash_array, Sequence) and dash_array:
            pen.setDashPattern([float(value) for value in dash_array])

        return pen

    # ------------------------------------------------------------------
    def is_layer_visible(self, layer_id: str, zoom: float) -> bool:
        """Check ``minzoom``/``maxzoom`` constraints and visibility flags."""

        layer = self._layers.get(layer_id)
        if not layer:
            return False

        minzoom = layer.get("minzoom")
        maxzoom = layer.get("maxzoom")
        if isinstance(minzoom, (int, float)) and zoom < float(minzoom):
            return False
        if isinstance(maxzoom, (int, float)) and zoom > float(maxzoom):
            return False

        visibility = layer.get("layout", {}).get("visibility", "visible")
        return visibility != "none"

    # ------------------------------------------------------------------
    def resolve_text_style(self, layer_id: str, zoom: float, properties: Dict[str, Any]) -> Optional[TextStyle]:
        """Build a :class:`TextStyle` instance for a symbol layer."""

        text = self.get_layout(layer_id, "text-field", zoom, properties)
        if not text:
            return None

        if isinstance(text, str):
            text = self._format_text(text, properties)

        if not text:
            return None

        size = self.get_layout(layer_id, "text-size", zoom, properties)
        if size is None:
            size = 12

        transform = self.get_layout(layer_id, "text-transform", zoom, properties)
        if isinstance(transform, str):
            if transform.lower() == "uppercase":
                text = text.upper()
            elif transform.lower() == "lowercase":
                text = text.lower()

        color = self.get_paint(layer_id, "text-color", zoom, properties)
        if not isinstance(color, QColor):
            color = QColor("black")

        halo_color = self.get_paint(layer_id, "text-halo-color", zoom, properties)
        if halo_color is not None and not isinstance(halo_color, QColor):
            halo_color = None

        halo_width = self.get_paint(layer_id, "text-halo-width", zoom, properties)
        if not isinstance(halo_width, (int, float)):
            halo_width = 0.0

        return TextStyle(text=text, size=float(size), color=color, halo_color=halo_color, halo_width=float(halo_width))

    # ------------------------------------------------------------------
    def _evaluate(self, value: Any, zoom: float, properties: Dict[str, Any]) -> Any:
        """Recursively evaluate style expressions."""

        if value is None:
            return None
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            if value.startswith("#"):
                return QColor(value)
            rgba = _COLOR_MATCHER.match(value)
            if rgba:
                components = [float(part.strip()) for part in rgba.group(1).split(",")]
                if len(components) == 4:
                    r, g, b, a = components
                    color = QColor(int(r), int(g), int(b))
                    color.setAlphaF(max(0.0, min(1.0, a)))
                    return color
            return value
        if isinstance(value, dict) and "stops" in value:
            return self._evaluate_stops(value["stops"], zoom)
        if isinstance(value, list):
            return self._evaluate_expression(value, zoom, properties)
        return value

    # ------------------------------------------------------------------
    def _evaluate_stops(self, stops: Any, zoom: float) -> Any:
        """Evaluate a MapLibre ``stops`` definition using linear interpolation."""

        if not stops:
            return None

        first_zoom, first_value = stops[0]
        if zoom <= first_zoom:
            return self._evaluate(first_value, zoom, {})

        for index in range(1, len(stops)):
            current_zoom, current_value = stops[index]
            previous_zoom, previous_value = stops[index - 1]
            if zoom <= current_zoom:
                fraction = (zoom - previous_zoom) / max(1e-9, (current_zoom - previous_zoom))
                start_value = self._evaluate(previous_value, zoom, {})
                end_value = self._evaluate(current_value, zoom, {})
                if isinstance(start_value, (int, float)) and isinstance(end_value, (int, float)):
                    return start_value + (end_value - start_value) * fraction
                return start_value

        _, last_value = stops[-1]
        return self._evaluate(last_value, zoom, {})

    # ------------------------------------------------------------------
    def _evaluate_expression(self, expression: list, zoom: float, properties: Dict[str, Any]) -> Any:
        """Handle a subset of MapLibre expressions.

        The expressions encountered in ``style.json`` mostly revolve around
        ``match`` and ``get`` operators.  Implementing the full expression
        system would add a lot of code.  Instead we focus on the expressions
        actually used by the bundled style, while making it straightforward to
        extend the interpreter when new expressions become necessary.
        """

        if not expression:
            return None

        operator = expression[0]
        if not isinstance(operator, str):
            # Literal lists such as dash arrays are represented as plain
            # sequences without an operator token. In that case we simply
            # return the raw list so the caller can interpret the numeric
            # values without the resolver emitting misleading warnings.
            return expression
        if operator == "match":
            input_value = self._evaluate(expression[1], zoom, properties)
            index = 2
            while index < len(expression) - 1:
                keys = expression[index]
                result = expression[index + 1]
                index += 2
                if isinstance(keys, list):
                    if input_value in keys:
                        return self._evaluate(result, zoom, properties)
                else:
                    if input_value == keys:
                        return self._evaluate(result, zoom, properties)
            if index < len(expression):
                return self._evaluate(expression[-1], zoom, properties)
            return None
        if operator == "get":
            key = expression[1]
            return properties.get(key)
        if operator == "literal" and len(expression) > 1:
            return expression[1]
        if operator == "step" and len(expression) > 2:
            # ``step`` works similar to ``stops`` but is provided as an
            # expression.  The structure is ``["step", input, default, stop1,
            # value1, stop2, value2, ...]``.  The bundled style does not use
            # this but it is inexpensive to implement.
            input_value = self._evaluate(expression[1], zoom, properties)
            result = self._evaluate(expression[2], zoom, properties)
            index = 3
            while index < len(expression) - 1:
                stop = expression[index]
                next_value = expression[index + 1]
                index += 2
                if isinstance(input_value, (int, float)) and input_value < stop:
                    return result
                result = self._evaluate(next_value, zoom, properties)
            return result
        if operator == "interpolate" and len(expression) > 3:
            # Only support ``interpolate(linear, [zoom], stop...)`` which is the
            # format produced by MapTiler for the provided style file.
            stops = []
            for index in range(3, len(expression), 2):
                if index + 1 < len(expression):
                    stop_zoom = self._evaluate(expression[index], zoom, properties)
                    stop_value = self._evaluate(expression[index + 1], zoom, properties)
                    stops.append((stop_zoom, stop_value))
            return self._evaluate_stops(stops, zoom)

        # Fallback for unknown expressions: return the raw list to avoid hiding
        # the configuration completely.  This makes it easier to spot missing
        # features while developing and testing the preview.
        _LOGGER.warning("Unsupported style expression encountered: %s", expression)
        return expression

    # ------------------------------------------------------------------
    @staticmethod
    def _format_text(template: str, properties: Dict[str, Any]) -> str:
        """Replace ``{field}`` placeholders with the corresponding property."""

        def replacer(match: re.Match[str]) -> str:
            key = match.group(1)
            value = properties.get(key, "")
            return str(value) if value is not None else ""

        return re.sub(r"\{([^}]+)\}", replacer, template)

    # ------------------------------------------------------------------
    def _evaluate_filter(self, expression: Sequence[Any], properties: Dict[str, Any]) -> bool:
        """Evaluate the subset of filter operators used by ``style.json``."""

        if not expression:
            return True

        operator = expression[0]
        if operator == "all":
            return all(self._evaluate_filter(sub, properties) for sub in expression[1:])
        if operator == "any":
            return any(self._evaluate_filter(sub, properties) for sub in expression[1:])
        if operator == "none":
            return not any(self._evaluate_filter(sub, properties) for sub in expression[1:])
        if operator == "==" and len(expression) >= 3:
            key = expression[1]
            return properties.get(key) == expression[2]
        if operator == "!=" and len(expression) >= 3:
            key = expression[1]
            return properties.get(key) != expression[2]
        if operator == "in" and len(expression) >= 3:
            key = expression[1]
            return properties.get(key) in expression[2:]
        if operator == "!in" and len(expression) >= 3:
            key = expression[1]
            return properties.get(key) not in expression[2:]
        if operator == "has" and len(expression) >= 2:
            key = expression[1]
            return key in properties and properties.get(key) is not None
        if operator == "!has" and len(expression) >= 2:
            key = expression[1]
            return key not in properties or properties.get(key) is None

        # Unknown operators default to ``False`` so we do not accidentally draw
        # layers with unexpected filters.  The diagnostic makes it easier to
        # spot missing implementations while developing locally.
        _LOGGER.warning("Unsupported filter operator '%s' in expression %s", operator, expression)
        return False


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the inclusive ``[0.0, 1.0]`` range."""

    return max(0.0, min(1.0, float(value)))


__all__ = ["StyleResolver", "TextStyle", "StyleLoadError"]
