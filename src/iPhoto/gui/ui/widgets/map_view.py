"""Interactive map view that visualises photo clusters by location."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from PySide6.QtCore import QObject, QUrl, Signal, Slot, Qt
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView


class _MapBridge(QObject):
    """Bridge object exposed to JavaScript via :class:`QWebChannel`.

    The bridge forwards click notifications from the web view back to the
    Python layer so the navigation controller can react to cluster selections.
    """

    clusterClicked = Signal(str)
    """Signal emitted when JavaScript reports a cluster click."""

    @Slot(str)
    def reportClusterClicked(self, location_name: str) -> None:
        """Receive a location identifier from JavaScript and relay it."""

        normalized = location_name.strip()
        if normalized:
            self.clusterClicked.emit(normalized)


class MapView(QWebEngineView):
    """Thin wrapper around :class:`QWebEngineView` that renders a Leaflet map."""

    clusterClicked = Signal(str)
    """Signal relayed when the user activates a cluster marker on the map."""

    def __init__(self, parent: QObject | None = None) -> None:
        """Initialise the widget and prepare the embedded web channel."""

        super().__init__(parent)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        self._bridge = _MapBridge(self)
        self._bridge.clusterClicked.connect(self.clusterClicked)

        channel = QWebChannel(self.page())
        channel.registerObject("qtBridge", self._bridge)
        self.page().setWebChannel(channel)
        self._channel = channel

        self._is_loaded: bool = False
        self._pending_clusters: List[Dict[str, Any]] = []

        self.loadFinished.connect(self._on_load_finished)
        self._load_map_document()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_photo_clusters(self, clusters: Iterable[Dict[str, Any]]) -> None:
        """Replace the rendered clusters with *clusters*.

        Parameters
        ----------
        clusters:
            An iterable of mapping objects describing each location cluster.  The
            dictionaries are expected to provide the following keys:

            ``location_name``
                Human readable title for the location.
            ``count``
                Number of assets associated with the location.
            ``lat`` / ``lon``
                Geographic coordinates expressed as floating point numbers.
            ``thumbnail_url``
                ``file://`` or ``http(s)://`` URL pointing to the preview image
                used for the marker.
        """

        self._pending_clusters = list(clusters)
        if self._is_loaded:
            self._push_clusters_to_js()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _load_map_document(self) -> None:
        """Load the bundled HTML document that bootstraps the Leaflet map."""

        html_path = Path(__file__).resolve().parent.parent / "assets" / "map.html"
        self.load(QUrl.fromLocalFile(str(html_path)))

    def _on_load_finished(self, ok: bool) -> None:
        """Handle the web view finishing its load cycle."""

        self._is_loaded = ok
        if ok:
            self._push_clusters_to_js()

    def _push_clusters_to_js(self) -> None:
        """Serialise the queued clusters and hand them to JavaScript."""

        try:
            payload = json.dumps(self._pending_clusters)
        except (TypeError, ValueError):
            payload = "[]"
        self.page().runJavaScript(f"window.updateClusters({payload});")


__all__ = ["MapView"]
