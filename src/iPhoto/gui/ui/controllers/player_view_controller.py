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

    completed = Signal(Path, QImage, bool)
    """Emitted when the adjusted image finished loading successfully.

    The boolean flag differentiates between the fast preview pass and the
    follow-up delivery of the full-resolution frame.  The player controller
    keeps the preview visible while it waits for the high fidelity version so
    the UI remains responsive during the extra decode work."""

    failed = Signal(Path, str)
    """Emitted when loading or processing the image fails."""


class _AdjustedImageWorker(QRunnable):
    """Load and tone-map an image on a background thread."""

    def __init__(
        self,
        source: Path,
        signals: _AdjustedImageSignals,
        preview_size: QSize | None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._source = source
        self._signals = signals
        # ``_preview_size`` bounds the initial low-resolution read that keeps
        # the detail view responsive.  The worker subsequently falls back to a
        # full-resolution decode whenever the preview had to downscale the
        # source, ensuring zoomed-in views retain their sharpness.
        self._preview_size = preview_size

    def run(self) -> None:  # pragma: no cover - executed on a worker thread
        """Perform the expensive image work outside the GUI thread."""

        try:
            adjustments = sidecar.load_adjustments(self._source)
        except Exception as exc:  # pragma: no cover - filesystem errors are rare
            self._signals.failed.emit(self._source, str(exc))
            return

        def _load_and_adjust(target: QSize | None) -> tuple[Optional[QImage], Optional[str]]:
            """Decode *target* and apply adjustments, returning an error message on failure."""

            try:
                image = image_loader.load_qimage(self._source, target)
            except Exception as exc:  # pragma: no cover - Qt loader errors are rare
                return None, str(exc)

            if image is None or image.isNull():
                return None, "Image decoder returned an empty frame"

            if adjustments:
                try:
                    image = apply_adjustments(image, adjustments)
                except Exception as exc:  # pragma: no cover - defensive safeguard
                    return None, str(exc)

            return image, None

        preview_image: Optional[QImage] = None
        preview_error: Optional[str] = None
        requires_full_decode = True

        if self._preview_size is not None:
            preview_target = self._preview_size
            if preview_target.isValid() and not preview_target.isEmpty():
                preview_image, preview_error = _load_and_adjust(preview_target)
                if preview_image is not None:
                    # Present the downscaled frame immediately so the detail
                    # view updates without waiting for the heavier full-size
                    # decode.  The caller keeps the preview visible until the
                    # subsequent completion marks the high-resolution image as
                    # ready.
                    self._signals.completed.emit(self._source, preview_image, True)
                    # ``QImageReader`` clamps at least one dimension to the
                    # requested bounding box when scaling occurs.  If both
                    # dimensions end up smaller than the provided preview size
                    # then the original already fit inside the viewport and no
                    # additional decode is required.
                    requires_full_decode = not (
                        preview_image.width() < preview_target.width()
                        and preview_image.height() < preview_target.height()
                    )
                else:
                    requires_full_decode = True
            else:
                # An invalid preview hint should not prevent the full
                # resolution image from loading.  Logically ignore it and
                # proceed with the unbounded decode path.
                requires_full_decode = True

        if not requires_full_decode and preview_image is not None:
            # The preview already matches the intrinsic asset size, so reuse it
            # as the "final" frame and avoid a redundant decode of the same
            # pixel data.
            self._signals.completed.emit(self._source, preview_image, False)
            return

        full_image, full_error = _load_and_adjust(None)
        if full_image is not None:
            self._signals.completed.emit(self._source, full_image, False)
            return

        if preview_image is not None:
            # Falling back to the preview image keeps something visible even if
            # the full-resolution pass fails (for example due to a truncated
            # RAW file).  The controller treats this as the terminal frame so it
            # can release any loading state while still preserving the user's
            # view.
            self._signals.completed.emit(self._source, preview_image, False)
            return

        message = full_error or preview_error or "Failed to decode image"
        self._signals.failed.emit(self._source, message)


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
        # ``_pending_sources`` keeps track of assets currently waiting on a
        # full-resolution decode.  Preview completions leave the entry in place
        # so the controller can distinguish between intermediary and final
        # frames when multiple signals arrive for the same source.
        self._pending_sources: Set[Path] = set()
        # ``_preview_displayed`` records which assets are presently showing a
        # preview-quality frame.  If the follow-up high-resolution decode fails
        # we leave the preview visible instead of clearing the viewer back to a
        # blank placeholder.
        self._preview_displayed: Set[Path] = set()

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
        self._pending_sources.clear()
        self._preview_displayed.clear()
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
        self._pending_sources.add(source)

        signals.completed.connect(self._on_adjusted_image_ready)
        signals.failed.connect(self._on_adjusted_image_failed)

        def _finalize_on_completion(
            img_source: Path, img: QImage, is_preview: bool
        ) -> None:
            """Release worker resources once the terminal frame arrives."""

            # Preview frames are intentionally ignored because the worker still
            # has to emit the full-resolution result.  Cleaning up at that
            # point would drop references that the background thread expects to
            # remain valid for its final signal emission, recreating the race
            # that originally crashed the zoom flow.
            if is_preview:
                return
            self._release_worker(worker)
            # ``deleteLater`` queues destruction on the GUI thread, ensuring the
            # worker never dereferences a signal object that has already been
            # freed when it posts the completion event.
            signals.deleteLater()

        def _finalize_on_failure(img_source: Path, message: str) -> None:
            """Release worker resources after a terminal failure."""

            self._release_worker(worker)
            signals.deleteLater()

        signals.completed.connect(_finalize_on_completion)
        signals.failed.connect(_finalize_on_failure)

        try:
            self._pool.start(worker)
        except RuntimeError as exc:  # pragma: no cover - thread pool exhaustion is rare
            self._release_worker(worker)
            self._pending_sources.discard(source)
            self._preview_displayed.discard(source)
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
    def _on_adjusted_image_ready(self, source: Path, image: QImage, is_preview: bool) -> None:
        """Render *image* when the matching worker completes successfully."""

        if self._loading_source != source and source not in self._pending_sources:
            return

        if image.isNull():
            if not is_preview:
                self._pending_sources.discard(source)
                self._preview_displayed.discard(source)
                if self._loading_source == source:
                    self._loading_source = None
                self._image_viewer.clear()
                self.imageLoadingFailed.emit(
                    source,
                    "Image decoder returned an empty frame",
                )
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            if not is_preview:
                self._pending_sources.discard(source)
                self._preview_displayed.discard(source)
                if self._loading_source == source:
                    self._loading_source = None
                self._image_viewer.clear()
                self.imageLoadingFailed.emit(
                    source,
                    "Failed to convert image to pixmap",
                )
            return

        self._image_viewer.set_pixmap(pixmap)
        self.show_image_surface()
        if is_preview:
            self._preview_displayed.add(source)
            return

        self._pending_sources.discard(source)
        self._preview_displayed.discard(source)
        if self._loading_source == source:
            self._loading_source = None

    def _on_adjusted_image_failed(self, source: Path, message: str) -> None:
        """Propagate worker failures while ensuring stale results are ignored."""

        if self._loading_source != source and source not in self._pending_sources:
            return

        self._pending_sources.discard(source)
        had_preview = source in self._preview_displayed
        self._preview_displayed.discard(source)
        if self._loading_source == source:
            self._loading_source = None
        if not had_preview:
            self._image_viewer.clear()
        self.imageLoadingFailed.emit(source, message)

    def _release_worker(self, worker: _AdjustedImageWorker) -> None:
        """Drop completed workers so the thread pool can reclaim resources."""

        if worker in self._active_workers:
            self._active_workers.remove(worker)
        worker.setAutoDelete(True)
