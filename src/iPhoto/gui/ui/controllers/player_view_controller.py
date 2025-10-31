"""Coordinator for the stacked player widgets used on the detail page."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from PySide6.QtCore import QObject, QRunnable, QSize, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QStackedWidget, QWidget

from ...utils import image_loader
from ....core.image_filters import apply_adjustments
from ....io import sidecar
from ..widgets.image_viewer import ImageViewer
from ..widgets.live_badge import LiveBadge
from ..widgets.video_area import VideoArea


class _AdjustedImageSignals(QObject):
    """Relay worker completion events back to the GUI thread."""

    completed = Signal(Path, QImage)
    """Emitted when the adjusted image finished loading successfully."""

    failed = Signal(Path, str)
    """Emitted when loading or processing the image fails."""


class _AdjustedImageWorker(QRunnable):
    """Load and tone-map an image on a background thread."""

    def __init__(
        self,
        source: Path,
        signals: _AdjustedImageSignals,
        target_size: QSize | None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._source = source
        self._signals = signals
        self._target_size = target_size

    def run(self) -> None:  # pragma: no cover - executed on a worker thread
        """Perform the expensive image work outside the GUI thread."""

        try:
            target = self._target_size
            if target is not None and (not target.isValid() or target.isEmpty()):
                # A defensive guard keeps Qt from attempting to scale to invalid
                # sizes when the caller could not determine a meaningful target.
                target = None
            image = image_loader.load_qimage(self._source, target)
        except Exception as exc:  # pragma: no cover - Qt loader errors are rare
            self._signals.failed.emit(self._source, str(exc))
            return

        if image is None or image.isNull():
            self._signals.failed.emit(
                self._source,
                "Image decoder returned an empty frame",
            )
            return

        try:
            adjustments = sidecar.load_adjustments(self._source)
        except Exception as exc:  # pragma: no cover - filesystem errors are rare
            self._signals.failed.emit(self._source, str(exc))
            return

        if adjustments:
            try:
                image = apply_adjustments(image, adjustments)
            except Exception as exc:  # pragma: no cover - defensive safeguard
                self._signals.failed.emit(self._source, str(exc))
                return

        self._signals.completed.emit(self._source, image)


class PlayerViewController(QObject):
    """Control which player surface is visible and manage related UI state."""

    liveReplayRequested = Signal()
    """Re-emitted when the image viewer asks to replay a Live Photo."""

    imageLoadingFailed = Signal(Path, str)
    """Emitted when a still image fails to load or post-process."""

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
        self._pool = QThreadPool.globalInstance()
        self._active_workers: Set[_AdjustedImageWorker] = set()
        self._loading_source: Optional[Path] = None

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
    def display_image(self, source: Path, *, placeholder: Optional[QPixmap] = None) -> bool:
        """Begin loading ``source`` asynchronously, returning scheduling success.

        Parameters
        ----------
        source:
            The asset that should appear in the detail viewer.
        placeholder:
            An optional pixmap that is displayed immediately while the worker
            recalculates the full-resolution image.  Supplying a placeholder is
            especially useful when returning from the edit view because it
            preserves the user's last preview instead of flashing a blank frame.
        """

        self._loading_source = source
        if placeholder is None or placeholder.isNull():
            # Without a placeholder we fall back to the traditional behaviour of
            # clearing stale content so the worker paints a fresh frame.
            self._image_viewer.clear()
        else:
            # Reusing the provided pixmap keeps the surface populated while the
            # asynchronous load runs, eliminating distracting flashes to the
            # placeholder panel.
            self._image_viewer.set_pixmap(placeholder)
        self.show_image_surface()

        signals = _AdjustedImageSignals()

        # Requesting an image that roughly matches the viewport dramatically
        # reduces decode and adjustment costs for high-resolution originals
        # while still allowing Pillow fallbacks to operate at full fidelity when
        # the size is unavailable.  We copy the ``QSize`` so worker threads never
        # access a widget instance from outside the GUI thread.
        viewport_size: QSize | None = None
        try:
            candidate = self._image_viewer.viewport_widget().size()
        except Exception:
            candidate = QSize()
        if candidate.isValid() and not candidate.isEmpty():
            viewport_size = QSize(candidate)
        else:
            fallback = self._player_stack.size()
            if fallback.isValid() and not fallback.isEmpty():
                viewport_size = QSize(fallback)

        worker = _AdjustedImageWorker(source, signals, viewport_size)
        self._active_workers.add(worker)

        signals.completed.connect(self._on_adjusted_image_ready)
        signals.failed.connect(self._on_adjusted_image_failed)
        signals.completed.connect(lambda *_: self._release_worker(worker))
        signals.failed.connect(lambda *_: self._release_worker(worker))
        signals.completed.connect(signals.deleteLater)
        signals.failed.connect(signals.deleteLater)

        try:
            self._pool.start(worker)
        except RuntimeError as exc:  # pragma: no cover - thread pool exhaustion is rare
            self._release_worker(worker)
            self._loading_source = None
            self.imageLoadingFailed.emit(source, str(exc))
            return False
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
    def _on_adjusted_image_ready(self, source: Path, image: QImage) -> None:
        """Render *image* when the matching worker completes successfully."""

        if self._loading_source != source:
            return

        self._loading_source = None

        if image.isNull():
            self._image_viewer.clear()
            self.imageLoadingFailed.emit(source, "Image decoder returned an empty frame")
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._image_viewer.clear()
            self.imageLoadingFailed.emit(source, "Failed to convert image to pixmap")
            return

        self._image_viewer.set_pixmap(pixmap)
        self.show_image_surface()

    def _on_adjusted_image_failed(self, source: Path, message: str) -> None:
        """Propagate worker failures while ensuring stale results are ignored."""

        if self._loading_source != source:
            return

        self._loading_source = None
        self._image_viewer.clear()
        self.imageLoadingFailed.emit(source, message)

    def _release_worker(self, worker: _AdjustedImageWorker) -> None:
        """Drop completed workers so the thread pool can reclaim resources."""

        if worker in self._active_workers:
            self._active_workers.remove(worker)
        worker.setAutoDelete(True)
