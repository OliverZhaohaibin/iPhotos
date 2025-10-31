"""Controller coordinating the edit view and non-destructive adjustments."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Optional

from PySide6.QtCore import QObject, QThreadPool, QRunnable, Signal, QTimer, Qt
from PySide6.QtGui import QImage, QPixmap

from ....core.preview_backends import PreviewBackend, PreviewSession, select_preview_backend
from ....io import sidecar
from ...utils import image_loader
from ..models.asset_model import AssetModel
from ..models.edit_session import EditSession
from ..tasks.thumbnail_loader import ThumbnailLoader
from ..ui_main_window import Ui_MainWindow
from .player_view_controller import PlayerViewController
from .view_controller import ViewController


_LOGGER = logging.getLogger(__name__)


class _PreviewSignals(QObject):
    """Signals emitted by :class:`_PreviewWorker` once processing completes."""

    finished = Signal(QImage, int)
    """Emitted with the adjusted image and the job identifier."""


class _PreviewWorker(QRunnable):
    """Execute preview rendering in a background thread.

    The worker forwards tone-mapping requests to the selected
    :class:`~iPhoto.core.preview_backends.PreviewBackend` to keep heavy lifting
    off the UI thread.  Holding a strong reference to the backend session allows
    hardware accelerated implementations to retain any allocated resources for
    the duration of the job.
    """

    def __init__(
        self,
        backend: PreviewBackend,
        session: PreviewSession,
        adjustments: Mapping[str, float],
        job_id: int,
        signals: _PreviewSignals,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._session = session
        # Capture the adjustment mapping at the moment the job is created so the
        # session can continue to evolve without affecting in-flight work.
        self._adjustments = dict(adjustments)
        self._job_id = job_id
        self._signals = signals

    def run(self) -> None:  # type: ignore[override]
        """Perform the tone-mapping work and notify listeners when done."""

        try:
            adjusted = self._backend.render(self._session, self._adjustments)
        except Exception:
            # Propagate failures by emitting a null image.  The controller will
            # discard outdated or invalid results, so surfacing ``None`` keeps
            # the UI responsive even if processing fails unexpectedly.
            adjusted = QImage()
        self._signals.finished.emit(adjusted, self._job_id)


class EditController(QObject):
    """Own the edit session state and synchronise UI widgets."""

    editingStarted = Signal(Path)
    """Emitted after a source asset has been loaded for editing."""

    editingFinished = Signal(Path)
    """Emitted once adjustments are saved and the detail view is restored."""

    def __init__(
        self,
        ui: Ui_MainWindow,
        view_controller: ViewController,
        player_view: PlayerViewController,
        playlist,
        asset_model: AssetModel,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._ui = ui
        self._view_controller = view_controller
        self._player_view = player_view
        self._playlist = playlist
        self._asset_model = asset_model
        self._thumbnail_loader: ThumbnailLoader = asset_model.thumbnail_loader()

        self._preview_backend: PreviewBackend = select_preview_backend()
        _LOGGER.info("Initialised edit preview backend: %s", self._preview_backend.tier_name)

        self._session: Optional[EditSession] = None
        self._base_image: Optional[QImage] = None
        self._current_source: Optional[Path] = None
        self._preview_session: Optional[PreviewSession] = None

        # Timer used to debounce expensive preview rendering so the UI thread
        # stays responsive while the user drags a slider continuously.
        self._preview_update_timer = QTimer(self)
        self._preview_update_timer.setSingleShot(True)
        self._preview_update_timer.setInterval(50)
        self._preview_update_timer.timeout.connect(self._start_preview_job)

        # ``QThreadPool`` dispatches background preview jobs, preventing the
        # heavy pixel processing from blocking the event loop.
        self._thread_pool = QThreadPool.globalInstance()
        # Monotonic identifier used to discard stale results from superseded
        # preview jobs.
        self._preview_job_id = 0
        # Keep strong references to workers until their completion callbacks run
        # so they are not garbage collected prematurely.
        self._active_preview_workers: set[_PreviewWorker] = set()

        ui.edit_reset_button.clicked.connect(self._handle_reset_clicked)
        ui.edit_done_button.clicked.connect(self._handle_done_clicked)
        ui.edit_adjust_action.triggered.connect(lambda checked: self._handle_mode_change("adjust", checked))
        ui.edit_crop_action.triggered.connect(lambda checked: self._handle_mode_change("crop", checked))

        playlist.currentChanged.connect(self._handle_playlist_change)
        playlist.sourceChanged.connect(lambda _path: self._handle_playlist_change())

        ui.edit_header_container.hide()

    # ------------------------------------------------------------------
    def begin_edit(self) -> None:
        """Enter the edit view for the playlist's current asset."""

        if self._view_controller.is_edit_view_active():
            return
        source = self._playlist.current_source()
        if source is None:
            return
        image = image_loader.load_qimage(source)
        if image is None or image.isNull():
            return

        preview_image = self._prepare_preview_image(image)
        self._base_image = preview_image
        self._current_source = source

        # Tear down any existing backend session before creating a new one.  This
        # protects against edge-cases where ``begin_edit`` is called repeatedly
        # without leaving the view first.
        if self._preview_session is not None:
            self._preview_backend.dispose_session(self._preview_session)
        self._preview_session = self._preview_backend.create_session(preview_image)

        adjustments = sidecar.load_adjustments(source)

        session = EditSession(self)
        session.set_values(adjustments, emit_individual=False)
        session.valuesChanged.connect(self._handle_session_changed)
        self._session = session

        self._ui.edit_sidebar.set_session(session)
        self._ui.edit_sidebar.refresh()
        # Display the unadjusted preview immediately so the user sees feedback
        # while the first recalculation runs in the background.
        self._ui.edit_image_viewer.set_pixmap(QPixmap.fromImage(preview_image))
        self._set_mode("adjust")
        self._start_preview_job()

        self._ui.detail_chrome_container.hide()
        self._ui.edit_header_container.show()
        self._view_controller.show_edit_view()

        self.editingStarted.emit(source)

    def leave_edit_mode(self) -> None:
        """Return to the standard detail view without persisting changes."""

        self._cancel_pending_previews()
        if not self._view_controller.is_edit_view_active():
            return
        self._ui.edit_sidebar.set_session(None)
        self._ui.edit_image_viewer.clear()
        self._ui.edit_header_container.hide()
        self._ui.detail_chrome_container.show()
        self._view_controller.show_detail_view()
        self._session = None
        self._base_image = None
        self._current_source = None
        if self._preview_session is not None:
            self._preview_backend.dispose_session(self._preview_session)
            self._preview_session = None

    # ------------------------------------------------------------------
    def _handle_session_changed(self, values: dict) -> None:
        del values  # Unused – the session already stores the authoritative mapping.
        # Debounce preview updates to avoid recalculating the entire image for
        # every incremental slider movement event.
        self._preview_update_timer.start()

    def _cancel_pending_previews(self) -> None:
        """Stop timers and invalidate outstanding preview work."""

        self._preview_update_timer.stop()
        # Incrementing the job identifier causes any in-flight worker results to
        # be ignored once they finish.
        self._preview_job_id += 1

    def _start_preview_job(self) -> None:
        """Queue a background task that recalculates the preview image."""

        if self._preview_session is None or self._session is None:
            self._ui.edit_image_viewer.clear()
            return

        self._preview_job_id += 1
        job_id = self._preview_job_id

        signals = _PreviewSignals()
        signals.finished.connect(self._on_preview_ready)

        if self._preview_backend.supports_realtime:
            # Hardware accelerated backends are fast enough to run synchronously
            # on the UI thread, so we render immediately and forward the result.
            try:
                image = self._preview_backend.render(
                    self._preview_session,
                    self._session.values(),
                )
            except Exception:
                image = QImage()
            self._on_preview_ready(image, job_id)
            return

        worker = _PreviewWorker(
            self._preview_backend,
            self._preview_session,
            self._session.values(),
            job_id,
            signals,
        )
        self._active_preview_workers.add(worker)
        signals.finished.connect(lambda *_: self._active_preview_workers.discard(worker))

        # Submitting the worker to the shared thread pool keeps resource usage
        # bounded even when the user adjusts multiple sliders rapidly.
        self._thread_pool.start(worker)

    def _prepare_preview_image(self, image: QImage) -> QImage:
        """Return an image optimised for preview rendering throughput.

        Applying adjustments to a 1:1 copy of the source file quickly becomes
        prohibitively expensive for high resolution assets.  The edit preview
        only needs to match the on-screen size, so the helper scales the source
        to the current viewer dimensions (or a conservative fallback) while
        preserving the aspect ratio.  The reduced pixel count keeps CPU based
        rendering responsive without sacrificing perceived quality.
        """

        viewport_size = None
        viewer = self._ui.edit_image_viewer

        # ``ImageViewer`` exposes its scroll area viewport for external event
        # filters.  Reusing that helper yields the exact drawable surface size
        # when the widget has already been laid out.
        if hasattr(viewer, "viewport_widget"):
            try:
                viewport = viewer.viewport_widget()
            except Exception:
                viewport = None
            if viewport is not None:
                size = viewport.size()
                if size.isValid() and not size.isEmpty():
                    viewport_size = size

        if viewport_size is None:
            size = viewer.size()
            if size.isValid() and not size.isEmpty():
                viewport_size = size

        # Fall back to a 1600px bounding box when layout information is not yet
        # available (for example the first time the edit view is opened).  The
        # limit is high enough to look crisp on typical displays while avoiding
        # the worst case performance hit of processing multi-tens-of-megapixel
        # originals on the CPU.
        max_width = 1600
        max_height = 1600
        if viewport_size is not None:
            max_width = max(1, viewport_size.width())
            max_height = max(1, viewport_size.height())

        if image.width() <= max_width and image.height() <= max_height:
            # The source already fits within the requested bounds.  Return a
            # detached copy so subsequent pixel operations never touch the
            # caller's instance.
            return QImage(image)

        return image.scaled(
            max_width,
            max_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _on_preview_ready(self, image: QImage, job_id: int) -> None:
        """Update the preview if the emitted job matches the latest request."""

        if job_id != self._preview_job_id:
            # A newer preview superseded this result.  Drop it silently so the
            # UI reflects the most recent slider state.
            return

        if image.isNull():
            self._ui.edit_image_viewer.clear()
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._ui.edit_image_viewer.clear()
            return

        self._ui.edit_image_viewer.set_pixmap(pixmap)

    def _handle_reset_clicked(self) -> None:
        if self._session is None:
            return
        # Stop any pending preview updates so the reset renders immediately.
        self._cancel_pending_previews()
        self._session.reset()

    def _handle_done_clicked(self) -> None:
        # Ensure no delayed preview runs after committing the adjustments.
        self._cancel_pending_previews()
        if self._session is None or self._current_source is None:
            self.leave_edit_mode()
            return
        adjustments = self._session.values()
        sidecar.save_adjustments(self._current_source, adjustments)
        self._refresh_thumbnail_cache(self._current_source)
        self.leave_edit_mode()
        self.editingFinished.emit(self._current_source)

    def _refresh_thumbnail_cache(self, source: Path) -> None:
        metadata = self._asset_model.source_model().metadata_for_absolute_path(source)
        if metadata is None:
            return
        rel_value = metadata.get("rel")
        if not rel_value:
            return
        rel = str(rel_value)
        source_model = self._asset_model.source_model()
        if hasattr(source_model, "invalidate_thumbnail"):
            source_model.invalidate_thumbnail(rel)
        self._thumbnail_loader.invalidate(rel)

    def _handle_mode_change(self, mode: str, checked: bool) -> None:
        if not checked:
            return
        self._set_mode(mode)

    def _set_mode(self, mode: str) -> None:
        if mode == "adjust":
            self._ui.edit_adjust_action.setChecked(True)
            self._ui.edit_crop_action.setChecked(False)
            self._ui.edit_sidebar.set_mode("adjust")
        else:
            self._ui.edit_adjust_action.setChecked(False)
            self._ui.edit_crop_action.setChecked(True)
            self._ui.edit_sidebar.set_mode("crop")

    def _handle_playlist_change(self) -> None:
        if self._view_controller.is_edit_view_active():
            self.leave_edit_mode()
