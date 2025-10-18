"""Composite widget that embeds the map preview and renders photo markers."""

from __future__ import annotations

from logging import getLogger
from pathlib import Path
from typing import Dict, Iterable, Optional, cast

from PySide6.QtCore import QObject, QRectF, Qt, QEvent, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QOffscreenSurface,
    QOpenGLContext,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QToolTip, QVBoxLayout, QWidget

from iPhotos.maps.map_widget.map_gl_widget import MapGLWidget
from iPhotos.maps.map_widget.map_widget import MapWidget
from iPhotos.maps.map_widget.map_renderer import CityAnnotation

from ....library.manager import GeotaggedAsset
from ..tasks.thumbnail_loader import ThumbnailLoader
from .marker_controller import MarkerController, _MarkerCluster


logger = getLogger(__name__)


def check_opengl_support() -> bool:
    """Return ``True`` when the system can create a basic OpenGL context."""

    try:
        # ``QOffscreenSurface`` keeps the detection lightweight by avoiding any
        # visible windows while still exercising the platform specific OpenGL
        # plumbing that the accelerated widget relies on.
        surface = QOffscreenSurface()
        surface.create()
        if not surface.isValid():
            return False

        context = QOpenGLContext()
        if not context.create():
            return False

        if not context.makeCurrent(surface):
            return False

        context.doneCurrent()
        return True
    except Exception:  # noqa: BLE001 - fall back gracefully on any Qt failure
        # Creating the surface or context can fail in virtualised or
        # misconfigured environments.  Returning ``False`` ensures the view
        # falls back to the CPU renderer instead of crashing.
        return False


class _MarkerLayer(QWidget):
    """Transparent overlay that paints thumbnail clusters with callout arrows."""

    MARKER_SIZE = 72
    THUMBNAIL_NATIVE_SIZE = 192
    THUMBNAIL_DISPLAY_SIZE = 56
    BADGE_DIAMETER = 26
    POINTER_HEIGHT = 10
    POINTER_WIDTH = 18
    CORNER_RADIUS = 12

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # The layer is purely visual, therefore it must not intercept input
        # events which are handled by :class:`PhotoMapView` and the map widget.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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

    def set_clusters(self, items: Iterable[_MarkerCluster]) -> None:
        """Replace the rendered clusters and schedule a repaint."""

        self._clusters = list(items)
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
        painter.end()

    def _paint_cluster(self, painter: QPainter, cluster: _MarkerCluster) -> None:
        width = float(self.MARKER_SIZE)
        display_edge = float(self.THUMBNAIL_DISPLAY_SIZE)
        # The callout should surround the thumbnail with an equal white border on all sides.
        # Deriving the border from the configured marker size keeps the geometry consistent
        # when designers tweak either constant while ensuring horizontal and vertical padding
        # always match.
        border = (width - display_edge) / 2.0
        body_height = display_edge + 2.0 * border
        height = body_height + float(self.POINTER_HEIGHT)
        x = cluster.screen_pos.x() - width / 2.0
        y = cluster.screen_pos.y() - height
        rect = QRectF(x, y, width, height)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        path = self._create_callout_path(rect)

        painter.save()
        painter.setPen(QPen(QColor(0, 0, 0, 80), 2))
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.drawPath(path)
        painter.restore()

        thumbnail = self._pixmaps.get(cluster.representative.library_relative)
        if thumbnail is None:
            thumbnail = self._placeholder
        if not thumbnail.isNull():
            thumb_rect = QRectF(
                rect.left() + border,
                rect.top() + border,
                display_edge,
                display_edge,
            )
            painter.save()
            clip_path = QPainterPath()
            # ``setClipPath`` trims the square pixmap into a rounded rectangle so
            # the map overlay mirrors the visual language used by the filmstrip
            # and the rest of the application.
            clip_path.addRoundedRect(thumb_rect, 8.0, 8.0)
            painter.setClipPath(clip_path, Qt.ClipOperation.ReplaceClip)
            painter.drawPixmap(thumb_rect.toRect(), thumbnail)
            painter.restore()

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

        cluster.bounding_rect = path.boundingRect()

    def _create_callout_path(self, rect: QRectF) -> QPainterPath:
        """Return a speech-bubble style path anchored at the rectangle centre."""

        path = QPainterPath()
        main_rect = QRectF(
            rect.left(),
            rect.top(),
            rect.width(),
            rect.height() - self.POINTER_HEIGHT,
        )
        path.addRoundedRect(main_rect, self.CORNER_RADIUS, self.CORNER_RADIUS)

        pointer_top = main_rect.bottom()
        pointer_center_x = main_rect.center().x()
        pointer_path = QPainterPath()
        pointer_path.moveTo(pointer_center_x, pointer_top + self.POINTER_HEIGHT)
        pointer_path.lineTo(pointer_center_x - self.POINTER_WIDTH / 2.0, pointer_top)
        pointer_path.lineTo(pointer_center_x + self.POINTER_WIDTH / 2.0, pointer_top)
        pointer_path.closeSubpath()

        return path.united(pointer_path)

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

    # ``assetActivated`` forwards the library-relative identifier of the asset
    # behind the activated marker.  Declaring the payload type as ``str`` keeps
    # Nuitka's signal analysis precise while documenting the contract for UI
    # controllers that listen for map selections.
    assetActivated = Signal(str)
    """Signal emitted when the user activates a single asset marker."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._map_widget: MapWidget | MapGLWidget

        if check_opengl_support():
            # Prefer the GPU accelerated widget when the platform exposes the
            # necessary OpenGL capabilities.  This dramatically improves pan and
            # zoom smoothness for larger libraries.
            self._map_widget = MapGLWidget(self)
            logger.info("Photo map initialised with GPU acceleration enabled.")
        else:
            # Fall back to the CPU implementation to keep the feature usable on
            # systems where OpenGL is unavailable or unstable.
            self._map_widget = MapWidget(self)
            logger.info("Photo map using CPU rendering because OpenGL is unavailable.")
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
        self._marker_controller.citiesUpdated.connect(self._handle_city_annotations)
        self._marker_controller.assetActivated.connect(self._on_marker_asset_activated)
        self._marker_controller.thumbnailUpdated.connect(self._overlay.set_thumbnail)
        self._marker_controller.thumbnailsInvalidated.connect(self._overlay.clear_pixmaps)
        self._last_tooltip_text = ""

    @Slot(str)
    def _on_marker_asset_activated(self, asset: str) -> None:
        """Relay marker activation events through :attr:`assetActivated`."""

        self.assetActivated.emit(asset)

    def map_widget(self) -> MapWidget | MapGLWidget:
        """Expose the underlying map widget for integration tests."""

        return self._map_widget

    def set_assets(self, assets: Iterable[GeotaggedAsset], library_root: Path) -> None:
        """Replace the asset catalogue shown on the map."""

        self._marker_controller.set_assets(assets, library_root)

    def clear(self) -> None:
        """Remove all markers from the map."""

        if self._last_tooltip_text:
            QToolTip.hideText()
            self._last_tooltip_text = ""
        self._marker_controller.clear()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._overlay.setGeometry(self._map_widget.geometry())
        self._marker_controller.handle_resize()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if watched is self._map_widget:
            if event.type() == QEvent.Type.MouseMove:
                mouse_event = cast(QMouseEvent, event)
                label = self._map_widget.city_at(mouse_event.position())
                if label:
                    global_pos = self._map_widget.mapToGlobal(mouse_event.position().toPoint())
                    if label != self._last_tooltip_text:
                        QToolTip.showText(global_pos, label, self._map_widget)
                        self._last_tooltip_text = label
                else:
                    if self._last_tooltip_text:
                        QToolTip.hideText()
                        self._last_tooltip_text = ""
            elif event.type() == QEvent.Type.Leave:
                if self._last_tooltip_text:
                    QToolTip.hideText()
                    self._last_tooltip_text = ""
            elif event.type() in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonDblClick,
            ):
                mouse_event = cast(QMouseEvent, event)
                cluster = self._marker_controller.cluster_at(mouse_event.position())
                if cluster is not None:
                    self._marker_controller.handle_marker_click(cluster)
                    return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Ensure background workers shut down before the widget closes."""

        if self._last_tooltip_text:
            QToolTip.hideText()
            self._last_tooltip_text = ""
        # ``MarkerController`` maintains a worker thread that aggregates marker clusters.
        # Explicitly shutting it down prevents the Qt event loop from waiting indefinitely.
        self._marker_controller.shutdown()
        # The map widget owns a ``TileManager`` that runs in a separate ``QThread`` to
        # stream map tiles.  If the thread is not told to exit, the application process
        # keeps running after the window closes, so we must always shut it down here.
        self._map_widget.shutdown()
        super().closeEvent(event)

    def _handle_city_annotations(self, cities: Iterable[CityAnnotation]) -> None:
        """Forward city annotations to the map widget for background rendering."""

        self._map_widget.set_city_annotations(list(cities))


__all__ = ["PhotoMapView"]
