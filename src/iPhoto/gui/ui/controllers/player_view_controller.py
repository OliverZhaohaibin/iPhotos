"""Coordinator for the stacked player widgets used on the detail page."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QStackedWidget, QWidget

from ...utils import image_loader
from ....core.image_filters import apply_adjustments
from ....io import sidecar
from ..widgets.image_viewer import ImageViewer
from ..widgets.live_badge import LiveBadge
from ..widgets.video_area import VideoArea


_LOGGER = logging.getLogger(__name__)


class _AdjustSignals(QObject):
    """Signals emitted by :class:`_AdjustWorker` when processing completes."""

    finished = Signal(QImage, str, int)
    """Emitted with the adjusted image, source path, and job identifier."""


class _AdjustWorker(QRunnable):
    """Apply sidecar adjustments off the UI thread to keep the UI responsive."""

    def __init__(
        self,
        base_image: QImage,
        adjustments: Mapping[str, float],
        signals: _AdjustSignals,
        source_path: Path,
        job_id: int,
    ) -> None:
        super().__init__()
        # ``QImage`` implements implicit sharing.  Copying here ensures the
        # worker operates on an independent buffer even if the caller mutates
        # their reference while this job executes.
        self._base_image = QImage(base_image)
        self._adjustments = dict(adjustments)
        self._signals = signals
        self._source_path = source_path
        self._job_id = job_id

    def run(self) -> None:  # type: ignore[override]
        """Perform the expensive tone-mapping work in a background thread."""

        try:
            adjusted = apply_adjustments(self._base_image, self._adjustments)
        except Exception:  # pragma: no cover - defensive logging path
            _LOGGER.exception("Failed to apply adjustments for %s", self._source_path)
            adjusted = QImage()
        # ``Path`` instances are serialised as strings to avoid Qt metatype
        # registration when crossing thread boundaries.
        self._signals.finished.emit(adjusted, str(self._source_path), self._job_id)


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

        # Adjustment jobs reuse the global thread pool so several requests can be
        # queued without creating an unbounded number of worker threads.
        self._thread_pool = QThreadPool.globalInstance()
        # Path for the asset currently shown in the image viewer.  The value is
        # compared against worker emissions to avoid flashing stale results when
        # the user navigates quickly between images.
        self._current_source_path: Optional[Path] = None
        # Incremented whenever ``display_image`` is called; workers include this
        # identifier in their ``finished`` signal so the controller can ignore
        # jobs that were superseded by newer navigation events.
        self._adjust_job_id = 0
        # Strong reference to the active worker prevents it from being garbage
        # collected while still queued in the thread pool.
        self._active_worker: Optional[_AdjustWorker] = None

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
        """Load ``source`` into the image viewer using asynchronous adjustments."""

        self._current_source_path = source
        # Increment the job identifier immediately so any pending worker results
        # are ignored if they emit after this call returns.
        self._adjust_job_id += 1
        job_id = self._adjust_job_id

        image = image_loader.load_qimage(source)
        if image is None or image.isNull():
            return False

        adjustments = sidecar.load_adjustments(source)

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return False

        # Show the base image instantly so users receive immediate feedback,
        # then let the worker update the view once the adjustments complete.
        self._image_viewer.set_pixmap(pixmap)
        self.show_image_surface()

        if not adjustments:
            # No adjustments recorded; nothing else to do.
            self._active_worker = None
            return True

        signals = _AdjustSignals()
        signals.finished.connect(self._handle_adjustment_finished)
        worker = _AdjustWorker(image, adjustments, signals, source, job_id)
        self._active_worker = worker
        self._thread_pool.start(worker)
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

    # ------------------------------------------------------------------
    # Worker callbacks
    # ------------------------------------------------------------------
    def _handle_adjustment_finished(self, image: QImage, source: str, job_id: int) -> None:
        """Update the viewer when a background adjustment job completes."""

        if job_id != self._adjust_job_id:
            # A newer call to ``display_image`` superseded this worker.  Ignore
            # the stale result so the viewer keeps showing the latest asset.
            return

        if self._current_source_path is None or str(self._current_source_path) != source:
            # Navigation switched to a different asset while the worker ran.
            return

        self._active_worker = None

        if image.isNull():
            _LOGGER.error("Adjustment worker for %s returned an empty image", source)
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            _LOGGER.error("Failed to convert adjusted image for %s into pixmap", source)
            return

        if self._player_stack.currentWidget() is self._image_viewer:
            self._image_viewer.set_pixmap(pixmap)
