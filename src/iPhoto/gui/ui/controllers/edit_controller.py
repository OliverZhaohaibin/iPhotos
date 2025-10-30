"""Controller coordinating the edit view and non-destructive adjustments."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from PySide6.QtCore import QObject, QThreadPool, QRunnable, Signal, QTimer
from PySide6.QtGui import QImage, QPixmap

from ....core.image_filters import apply_adjustments
from ....io import sidecar
from ...utils import image_loader
from ..models.asset_model import AssetModel
from ..models.edit_session import EditSession
from ..tasks.thumbnail_loader import ThumbnailLoader
from ..ui_main_window import Ui_MainWindow
from .player_view_controller import PlayerViewController
from .view_controller import ViewController


class _PreviewSignals(QObject):
    """Signals emitted by :class:`_PreviewWorker` once processing completes."""

    finished = Signal(QImage, int)
    """Emitted with the adjusted image and the job identifier."""


class _PreviewWorker(QRunnable):
    """Execute ``apply_adjustments`` in a background thread.

    The worker mirrors the main-thread logic but avoids blocking the user
    interface.  Each instance carries an immutable snapshot of the adjustment
    values and emits the resulting image via :class:`_PreviewSignals` when the
    computation completes.
    """

    def __init__(
        self,
        image: QImage,
        adjustments: Mapping[str, float],
        job_id: int,
        signals: _PreviewSignals,
    ) -> None:
        super().__init__()
        # ``QImage`` is implicitly shared and therefore cheap to copy by
        # reference.  The worker stores the reference so the pixel data remains
        # accessible within the background thread for the duration of the run.
        self._image = image
        # Capture the adjustment mapping at the moment the job is created so the
        # session can continue to evolve without affecting in-flight work.
        self._adjustments = dict(adjustments)
        self._job_id = job_id
        self._signals = signals

    def run(self) -> None:  # type: ignore[override]
        """Perform the tone-mapping work and notify listeners when done."""

        try:
            adjusted = apply_adjustments(self._image, self._adjustments)
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

        self._session: Optional[EditSession] = None
        self._base_image: Optional[QImage] = None
        self._current_source: Optional[Path] = None

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

        self._base_image = image
        self._current_source = source

        adjustments = sidecar.load_adjustments(source)

        session = EditSession(self)
        session.set_values(adjustments, emit_individual=False)
        session.valuesChanged.connect(self._handle_session_changed)
        self._session = session

        self._ui.edit_sidebar.set_session(session)
        self._ui.edit_sidebar.refresh()
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

        if self._base_image is None or self._session is None:
            self._ui.edit_image_viewer.clear()
            return

        self._preview_job_id += 1
        job_id = self._preview_job_id

        signals = _PreviewSignals()
        signals.finished.connect(self._on_preview_ready)

        worker = _PreviewWorker(self._base_image, self._session.values(), job_id, signals)
        self._active_preview_workers.add(worker)
        signals.finished.connect(lambda *_: self._active_preview_workers.discard(worker))

        # Submitting the worker to the shared thread pool keeps resource usage
        # bounded even when the user adjusts multiple sliders rapidly.
        self._thread_pool.start(worker)

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
        self._player_view.display_image(self._current_source)
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
