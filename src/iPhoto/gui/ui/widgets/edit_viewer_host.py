"""Wrapper widget that allows reusing the GL viewer inside the edit workflow."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from .gl_image_viewer import GLImageViewer


class EditViewerHost(QWidget):
"""Bridge :class:`GLImageViewer` into the edit UI without duplicating uploads."""

    replayRequested = Signal()
    """Forwarded when the embedded viewer asks to replay a Live Photo."""

    zoomChanged = Signal(float)
    """Emitted whenever the zoom factor changes on the shared viewer."""

    nextItemRequested = Signal()
    """Forwarded wheel gesture request for the next asset."""

    prevItemRequested = Signal()
    """Forwarded wheel gesture request for the previous asset."""

    fullscreenExitRequested = Signal()
    """Forwarded when immersive mode should close and restore chrome."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._viewer: GLImageViewer | None = None
        self._last_pixmap: Optional[QPixmap] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._loading_overlay = QLabel("Loading…", self)
        self._loading_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._loading_overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 128); color: white; font-size: 18px;"
        )
        self._loading_overlay.hide()

    # ------------------------------------------------------------------
    # Viewer lifecycle
    # ------------------------------------------------------------------
    def adopt_viewer(self, viewer: GLImageViewer) -> None:
        """Embed *viewer* inside this host, wiring signal relays as needed."""

        if self._viewer is viewer:
            return
        self.release_viewer()
        self._viewer = viewer
        viewer.setParent(self)
        viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = self.layout()
        if layout is not None:
            layout.insertWidget(0, viewer)
        self._loading_overlay.raise_()
        self._loading_overlay.resize(self.size())
        viewer.replayRequested.connect(self.replayRequested)
        viewer.zoomChanged.connect(self.zoomChanged)
        viewer.nextItemRequested.connect(self.nextItemRequested)
        viewer.prevItemRequested.connect(self.prevItemRequested)
        viewer.fullscreenExitRequested.connect(self.fullscreenExitRequested)

    def release_viewer(self) -> GLImageViewer | None:
        """Detach the currently hosted viewer so it can return to the detail view."""

        viewer = self._viewer
        if viewer is None:
            return None
        viewer.replayRequested.disconnect(self.replayRequested)
        viewer.zoomChanged.disconnect(self.zoomChanged)
        viewer.nextItemRequested.disconnect(self.nextItemRequested)
        viewer.prevItemRequested.disconnect(self.prevItemRequested)
        viewer.fullscreenExitRequested.disconnect(self.fullscreenExitRequested)
        layout = self.layout()
        if layout is not None:
            layout.removeWidget(viewer)
        viewer.setParent(None)
        self._viewer = None
        return viewer

    def viewport_widget(self) -> GLImageViewer | None:
        """Expose the hosted GL widget so preview sizing can mirror the old API."""

        return self._viewer

    # ------------------------------------------------------------------
    # Rendering helpers matching :class:`ImageViewer`
    # ------------------------------------------------------------------
    def set_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        """Display *pixmap* using the shared GL viewer."""

        if pixmap is None or pixmap.isNull():
            self._last_pixmap = None
            self.clear()
            return
        self._last_pixmap = QPixmap(pixmap)
        if self._viewer is not None:
            self._viewer.set_image(pixmap.toImage(), {})

    def pixmap(self) -> Optional[QPixmap]:
        """Return a defensive copy of the last rendered pixmap."""

        if self._last_pixmap is None or self._last_pixmap.isNull():
            return None
        return QPixmap(self._last_pixmap)

    def clear(self) -> None:
        """Reset the hosted viewer to an empty state."""

        if self._viewer is not None:
            self._viewer.set_image(None, {})
        self._last_pixmap = None

    def set_loading(self, loading: bool) -> None:
        """Toggle the translucent loading overlay used during decode work."""

        self._loading_overlay.setVisible(bool(loading))
        if not loading:
            self._loading_overlay.hide()
        else:
            self._loading_overlay.raise_()

    def reset_zoom(self) -> None:
        if self._viewer is not None:
            self._viewer.reset_zoom()

    def zoom_in(self) -> None:
        if self._viewer is not None:
            self._viewer.zoom_in()

    def zoom_out(self) -> None:
        if self._viewer is not None:
            self._viewer.zoom_out()

    def set_zoom(self, factor: float, anchor: Optional[QPointF] = None) -> None:
        if self._viewer is not None:
            self._viewer.set_zoom(factor, anchor=anchor)

    def viewport_center(self) -> QPointF:
        if self._viewer is not None:
            return self._viewer.viewport_center()
        return QPointF(self.width() / 2.0, self.height() / 2.0)

    def set_surface_color_override(self, colour: str | None) -> None:
        if self._viewer is not None:
            self._viewer.set_surface_color_override(colour)

    def set_immersive_background(self, immersive: bool) -> None:
        if self._viewer is not None:
            self._viewer.set_immersive_background(immersive)

    def set_wheel_action(self, action: str) -> None:
        if self._viewer is not None:
            self._viewer.set_wheel_action(action)

    def set_live_replay_enabled(self, enabled: bool) -> None:
        if self._viewer is not None:
            self._viewer.set_live_replay_enabled(enabled)

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._loading_overlay is not None:
            self._loading_overlay.resize(self.size())


__all__ = ["EditViewerHost"]

