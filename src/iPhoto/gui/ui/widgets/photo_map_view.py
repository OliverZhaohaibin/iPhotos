"""Composite widget that embeds the map preview and renders photo markers."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Optional, Union, cast

from PySide6.QtCore import QObject, QRectF, Qt, QEvent, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QVBoxLayout, QWidget

from iPhotos.maps.map_widget.map_widget import MapWidget

from ....library.manager import GeotaggedAsset
from ..tasks.thumbnail_loader import ThumbnailLoader
from .marker_controller import MarkerController, _MarkerCluster, _CityMarker


class _MarkerLayer(QWidget):
    """Transparent overlay that paints thumbnails for marker clusters."""

    MARKER_SIZE = 72
    THUMBNAIL_NATIVE_SIZE = 192
    THUMBNAIL_DISPLAY_SIZE = 56
    BADGE_DIAMETER = 26

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # The layer is purely visual, therefore it must not intercept input
        # events which are handled by :class:`PhotoMapView` and the map widget.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._clusters: list[_MarkerCluster] = []
        self._cities: list[_CityMarker] = []
        self._pixmaps: Dict[str, QPixmap] = {}
        self._placeholder = self._create_placeholder()
        self._badge_font = QFont()
        self._badge_font.setBold(True)
        self._badge_pen = QPen(QColor("white"))
        self._badge_pen.setWidth(1)
        self._badge_brush = QColor("#d64541")
        self._city_font = QFont()
        self._city_font.setPointSize(12)
        self._city_font.setBold(True)

    @property
    def marker_size(self) -> int:
        """Return the logical footprint of each marker."""

        return self.MARKER_SIZE

    @property
    def thumbnail_size(self) -> int:
        """Return the requested thumbnail edge length."""

        return self.THUMBNAIL_NATIVE_SIZE

    @property
    def thumbnail_display_size(self) -> int:
        """Return the on-screen pixel edge length used for thumbnails."""

        return self.THUMBNAIL_DISPLAY_SIZE

    def set_clusters(self, items: Iterable[Union[_MarkerCluster, _CityMarker]]) -> None:
        """Replace the rendered markers and schedule a repaint."""

        self._clusters = [item for item in items if isinstance(item, _MarkerCluster)]
        self._cities = [item for item in items if isinstance(item, _CityMarker)]
        self.update()

    def set_thumbnail(self, rel: str, pixmap: QPixmap) -> None:
        """Cache the pixmap associated with *rel* and refresh the overlay."""

        if pixmap.isNull():
            return
        self._pixmaps[rel] = pixmap
        self.update()

    def clear_pixmaps(self) -> None:
        """Drop cached pixmaps so outdated thumbnails are not reused."""

        self._pixmaps.clear()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for cluster in self._clusters:
            self._paint_cluster(painter, cluster)
        for city in self._cities:
            self._paint_city(painter, city)
        painter.end()

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

    def _paint_city(self, painter: QPainter, city: _CityMarker) -> None:
        """Render a macOS-style city bubble anchored at ``city.screen_pos``."""

        painter.save()
        painter.setFont(self._city_font)
        metrics = QFontMetrics(self._city_font)

        # Measure the text to determine how much padding we need around the
        # label. The additional offsets mimic the rounded callout used by the
        # macOS Maps application.
        text_rect = metrics.boundingRect(city.name)
        horizontal_padding = 8
        vertical_padding = 4
        icon_size = 8
        icon_spacing = 4
        pointer_height = 8
        pointer_width = 12

        box_width = text_rect.width() + icon_size + icon_spacing + 2 * horizontal_padding
        box_height = max(text_rect.height(), icon_size) + 2 * vertical_padding

        tip_x = city.screen_pos.x()
        tip_y = city.screen_pos.y()
        box_x = tip_x - box_width / 2.0
        box_y = tip_y - box_height - pointer_height
        box_rect = QRectF(box_x, box_y, box_width, box_height)

        # Compose the callout path with a rounded rectangle and a triangular
        # pointer. Using a single path ensures the outline blends smoothly.
        bubble_path = QPainterPath()
        bubble_path.addRoundedRect(box_rect, 6, 6)
        pointer_path = QPainterPath()
        pointer_path.moveTo(tip_x, tip_y)
        pointer_path.lineTo(tip_x - pointer_width / 2.0, tip_y - pointer_height)
        pointer_path.lineTo(tip_x + pointer_width / 2.0, tip_y - pointer_height)
        pointer_path.closeSubpath()
        bubble_path = bubble_path.united(pointer_path)

        painter.setPen(QPen(QColor(0, 0, 0, 80), 1))
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.drawPath(bubble_path)

        # Draw the leading circular icon to visually differentiate the marker
        # from photo thumbnails.
        icon_x = box_x + horizontal_padding
        icon_y = box_y + (box_height - icon_size) / 2.0
        icon_rect = QRectF(icon_x, icon_y, icon_size, icon_size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1e73ff"))
        painter.drawEllipse(icon_rect)

        text_x = icon_rect.right() + icon_spacing
        text_rect_final = QRectF(text_x, box_y, box_width - (text_x - box_x), box_height)
        painter.setPen(QColor("#2b2b2b"))
        painter.drawText(text_rect_final, Qt.AlignmentFlag.AlignCenter, city.name)

        city.bounding_rect = bubble_path.boundingRect()
        painter.restore()

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

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._map_widget = MapWidget(self)
        layout.addWidget(self._map_widget)

        self._overlay = _MarkerLayer(self)
        self._overlay.setGeometry(self._map_widget.geometry())
        self._overlay.raise_()

        self._map_widget.installEventFilter(self)

        self._thumbnail_loader = ThumbnailLoader(self)
        self._marker_controller = MarkerController(
            self._map_widget,
            self._thumbnail_loader,
            marker_size=self._overlay.marker_size,
            thumbnail_size=self._overlay.thumbnail_size,
            parent=self,
        )

        self._map_widget.viewChanged.connect(self._marker_controller.handle_view_changed)
        self._map_widget.panned.connect(self._marker_controller.handle_pan)
        self._map_widget.panFinished.connect(self._marker_controller.handle_pan_finished)
        self._thumbnail_loader.ready.connect(self._marker_controller.handle_thumbnail_ready)
        self._marker_controller.clustersUpdated.connect(self._overlay.set_clusters)
        self._marker_controller.assetActivated.connect(self.assetActivated.emit)
        self._marker_controller.thumbnailUpdated.connect(self._overlay.set_thumbnail)
        self._marker_controller.thumbnailsInvalidated.connect(self._overlay.clear_pixmaps)

    def map_widget(self) -> MapWidget:
        """Expose the underlying :class:`MapWidget` for integration tests."""

        return self._map_widget

    def set_assets(self, assets: Iterable[GeotaggedAsset], library_root: Path) -> None:
        """Replace the asset catalogue shown on the map."""

        self._marker_controller.set_assets(assets, library_root)

    def clear(self) -> None:
        """Remove all markers from the map."""

        self._marker_controller.clear()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._overlay.setGeometry(self._map_widget.geometry())
        self._marker_controller.handle_resize()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if watched is self._map_widget:
            if event.type() in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonDblClick,
            ):
                mouse_event = cast(QMouseEvent, event)
                item = self._marker_controller.cluster_at(mouse_event.position())
                if isinstance(item, _MarkerCluster):
                    self._marker_controller.handle_marker_click(item)
                    return True
                if isinstance(item, _CityMarker):
                    self._marker_controller.handle_city_click(item)
                    return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Ensure the clustering thread shuts down before the widget closes."""

        self._marker_controller.shutdown()
        super().closeEvent(event)


__all__ = ["PhotoMapView"]
