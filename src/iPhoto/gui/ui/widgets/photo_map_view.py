"""Composite widget that embeds the map preview and renders photo markers."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Optional, Union, cast

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QEvent, Signal
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
    """Transparent overlay that paints thumbnails and lightweight city labels."""

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
        for city in self._cities:
            self._paint_city(painter, city)
        for cluster in self._clusters:
            self._paint_cluster(painter, cluster)
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
        """Draw a lightweight city label directly on the map background."""

        painter.save()
        painter.setFont(self._city_font)
        metrics = QFontMetrics(self._city_font)
        text = city.name

        # ``dot_radius`` and ``text_gap`` control the footprint of the circular
        # anchor and the spacing between the dot and the caption.  Keeping the
        # geometry compact helps the overlay blend with the native map labels.
        dot_radius = 4.0
        text_gap = 6.0

        # Vertically center the baseline around the geographic anchor so the
        # dot remains aligned with the label's midpoint.
        text_height = float(metrics.height())
        text_top = city.screen_pos.y() - text_height / 2.0
        baseline_y = text_top + float(metrics.ascent())
        text_x = city.screen_pos.x() + dot_radius + text_gap

        # Render the dot with a subtle stroke so it stays legible on top of
        # both light and dark map styles.
        dot_pen = QPen(QColor("#f7fbff"))
        dot_pen.setWidthF(2.0)
        dot_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(dot_pen)
        painter.setBrush(QColor("#1e73ff"))
        painter.drawEllipse(city.screen_pos, dot_radius, dot_radius)

        # Draw a halo behind the label text to improve contrast against the map
        # tiles while preserving a lightweight appearance.
        text_path = QPainterPath()
        text_path.addText(QPointF(text_x, baseline_y), self._city_font, text)
        label_halo_pen = QPen(QColor(255, 255, 255, 220))
        label_halo_pen.setWidthF(3.0)
        label_halo_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        label_halo_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(label_halo_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(text_path)

        # Finally render the label itself in a muted tone similar to the native
        # cartography.
        painter.setPen(QColor("#2b2b2b"))
        painter.drawText(QPointF(text_x, baseline_y), text)

        text_width = float(metrics.horizontalAdvance(text))
        left = city.screen_pos.x() - dot_radius
        top = min(text_top, city.screen_pos.y() - dot_radius)
        right = text_x + text_width
        bottom = max(text_top + text_height, city.screen_pos.y() + dot_radius)
        city.bounding_rect = QRectF(left - 2.0, top - 2.0, (right - left) + 4.0, (bottom - top) + 4.0)
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
