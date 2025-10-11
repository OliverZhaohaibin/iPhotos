"""Coordinator for the stacked player widgets used on the detail page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QStackedWidget, QWidget

from ...utils import image_loader
from ..widgets.image_viewer import ImageViewer
from ..widgets.live_badge import LiveBadge
from ..widgets.video_area import VideoArea


class PlayerViewController(QObject):
    """Control which player surface is visible and manage related UI state."""

    liveReplayRequested = Signal()
    """Re-emitted when the image viewer asks to replay a Live Photo."""

    def __init__(
        self,
        player_stack: QStackedWidget,
        image_viewer: ImageViewer,
        video_area: VideoArea,
        placeholder: QWidget,
        live_badge: LiveBadge,
        parent: QObject | None = None,
    ) -> None:
        """Store references to the widgets composing the player area."""

        super().__init__(parent)
        self._player_stack = player_stack
        self._image_viewer = image_viewer
        self._video_area = video_area
        self._placeholder = placeholder
        self._live_badge = live_badge
        self._image_viewer.replayRequested.connect(self.liveReplayRequested)

    # ------------------------------------------------------------------
    # High-level surface selection helpers
    # ------------------------------------------------------------------
    def show_placeholder(self) -> None:
        """Display the placeholder widget and clear any previous image."""

        self._video_area.hide_controls(animate=False)
        self.hide_live_badge()
        if self._player_stack.currentWidget() is not self._placeholder:
            self._player_stack.setCurrentWidget(self._placeholder)
        if not self._player_stack.isVisible():
            self._player_stack.show()
        self._image_viewer.clear()

    def show_image_surface(self) -> None:
        """Reveal the still-image viewer surface."""

        self._video_area.hide_controls(animate=False)
        if self._player_stack.currentWidget() is not self._image_viewer:
            self._player_stack.setCurrentWidget(self._image_viewer)
        if not self._player_stack.isVisible():
            self._player_stack.show()

    def show_video_surface(self, *, interactive: bool) -> None:
        """Reveal the video surface, toggling playback controls as needed."""

        if self._player_stack.currentWidget() is not self._video_area:
            self._player_stack.setCurrentWidget(self._video_area)
        if not self._player_stack.isVisible():
            self._player_stack.show()
        self._video_area.set_controls_enabled(interactive)
        if interactive:
            self._video_area.show_controls(animate=False)
        else:
            self._video_area.hide_controls(animate=False)

    # ------------------------------------------------------------------
    # Content helpers
    # ------------------------------------------------------------------
    def display_image(self, source: Path) -> bool:
        """Load ``source`` into the image viewer, returning success."""

        pixmap = image_loader.load_qpixmap(source)
        if pixmap is None:
            return False
        self._image_viewer.set_pixmap(pixmap)
        self.show_image_surface()
        return True

    def clear_image(self) -> None:
        """Remove any pixmap currently shown in the image viewer."""

        self._image_viewer.clear()

    # ------------------------------------------------------------------
    # Live badge helpers
    # ------------------------------------------------------------------
    def show_live_badge(self) -> None:
        """Ensure the Live Photo badge is visible and raised above overlays."""

        self._live_badge.show()
        self._live_badge.raise_()

    def hide_live_badge(self) -> None:
        """Hide the Live Photo badge."""

        self._live_badge.hide()

    def is_live_badge_visible(self) -> bool:
        """Return ``True`` when the Live Photo badge is currently visible."""

        return self._live_badge.isVisible()

    # ------------------------------------------------------------------
    # Convenience wrappers used by the playback controller
    # ------------------------------------------------------------------
    def set_live_replay_enabled(self, enabled: bool) -> None:
        """Delegate Live Photo replay toggling to the image viewer."""

        self._image_viewer.set_live_replay_enabled(enabled)

    def is_showing_video(self) -> bool:
        """Return ``True`` when the video surface is the current widget."""

        return self._player_stack.currentWidget() is self._video_area

    def is_showing_image(self) -> bool:
        """Return ``True`` when the still-image surface is active."""

        return self._player_stack.currentWidget() is self._image_viewer

    def note_video_activity(self) -> None:
        """Forward external activity notifications to the video controls."""

        self._video_area.note_activity()

    @property
    def image_viewer(self) -> ImageViewer:
        """Expose the image viewer for read-only integrations."""

        return self._image_viewer

    @property
    def video_area(self) -> VideoArea:
        """Expose the video area for media output bindings."""

        return self._video_area
