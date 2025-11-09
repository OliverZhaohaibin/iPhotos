"""Preview rendering coordinator extracted from :mod:`edit_controller`."""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from PySide6.QtCore import QObject, QThreadPool, QTimer, Qt, Signal, QSize
from PySide6.QtGui import QImage, QPixmap

from ....core.light_resolver import LIGHT_KEYS, resolve_light_vector
from ....core.color_resolver import (
    COLOR_KEYS,
    ColorResolver,
    ColorStats,
    compute_color_statistics,
)
from ....core.preview_backends import (
    PreviewBackend,
    PreviewSession,
    fallback_preview_backend,
    select_preview_backend,
)
from ..widgets.image_viewer import ImageViewer
from ..tasks.preview_render_worker import PreviewRenderWorker

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adjustment helpers
# ---------------------------------------------------------------------------

def resolve_adjustment_mapping(
    session_values: Mapping[str, float | bool],
    *,
    stats: ColorStats | None = None,
) -> dict[str, float]:
    """Return shader-friendly adjustments derived from *session_values*.

    The helper mirrors the Photos-compatible colour math used by the CPU preview
    renderer so both the OpenGL shader and the background pixmap pipeline apply
    identical transformations.  Passing a :class:`ColorStats` instance ensures
    colour tools such as the Cast slider honour the white balance sampled from
    the source frame while keeping the function side-effect free for callers
    that do not have statistics available yet.
    """

    resolved: dict[str, float] = {}
    overrides: dict[str, float] = {}
    color_overrides: dict[str, float] = {}

    master_value = float(session_values.get("Light_Master", 0.0))
    light_enabled = bool(session_values.get("Light_Enabled", True))
    color_master = float(session_values.get("Color_Master", 0.0))
    color_enabled = bool(session_values.get("Color_Enabled", True))

    for key, value in session_values.items():
        if key in ("Light_Master", "Light_Enabled", "Color_Master", "Color_Enabled"):
            continue
        if key == "BW_Master":
            continue
        if key in LIGHT_KEYS:
            overrides[key] = float(value)
        elif key in COLOR_KEYS:
            color_overrides[key] = float(value)
        else:
            resolved[key] = float(value)

    if light_enabled:
        resolved.update(resolve_light_vector(master_value, overrides, mode="delta"))
    else:
        resolved.update({key: 0.0 for key in LIGHT_KEYS})

    stats_obj = stats or ColorStats()
    if color_enabled:
        resolved.update(
            ColorResolver.resolve_color_vector(
                color_master,
                color_overrides,
                stats=stats_obj,
                mode="delta",
            )
        )
    else:
        resolved.update({key: 0.0 for key in COLOR_KEYS})

    gain_r, gain_g, gain_b = stats_obj.white_balance_gain
    resolved["Color_Gain_R"] = float(gain_r)
    resolved["Color_Gain_G"] = float(gain_g)
    resolved["Color_Gain_B"] = float(gain_b)

    # Preserve the dedicated Black & White parameters so the GPU and CPU pipelines share the
    # updated shader-compatible values.
    bw_enabled = bool(session_values.get("BW_Enabled", False))
    # The shader receives a dedicated toggle so it can entirely skip the costly
    # monochrome branch when the effect is disabled.  Expressing the boolean as
    # a float keeps the adjustment dictionary homogeneous for downstream code
    # that expects numeric uniform values.
    resolved["BWEnabled"] = 1.0 if bw_enabled else 0.0

    if bw_enabled:
        resolved["BWIntensity"] = float(session_values.get("BW_Intensity", 0.5))
        resolved["BWNeutrals"] = float(session_values.get("BW_Neutrals", 0.0))
        resolved["BWTone"] = float(session_values.get("BW_Tone", 0.0))
        resolved["BWGrain"] = float(session_values.get("BW_Grain", 0.0))
    else:
        resolved["BWIntensity"] = 0.5
        resolved["BWNeutrals"] = 0.0
        resolved["BWTone"] = 0.0
        resolved["BWGrain"] = 0.0

    return resolved


class EditPreviewManager(QObject):
    """Own preview rendering resources for the edit workflow."""

    preview_updated = Signal(QPixmap)
    """Emitted when a recalculated preview pixmap is ready for display."""

    image_cleared = Signal()
    """Emitted when no preview image is available and the viewer should clear."""

    def __init__(self, viewer: ImageViewer, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._viewer = viewer
        backend = select_preview_backend()
        if backend.supports_realtime:
            # Hardware accelerated backends expect to run on the GUI thread.  The
            # edit workflow now renders previews on a thread pool to keep the UI
            # responsive, so fall back to the CPU implementation when required.
            fallback = fallback_preview_backend(backend)
            if fallback is not backend:
                _LOGGER.info(
                    "Switching preview backend from %s to %s for threaded rendering",
                    backend.tier_name,
                    fallback.tier_name,
                )
                backend = fallback
        self._preview_backend: PreviewBackend = backend
        _LOGGER.info("Initialised edit preview backend: %s", self._preview_backend.tier_name)

        self._preview_session: Optional[PreviewSession] = None
        self._base_image: Optional[QImage] = None
        self._base_pixmap: Optional[QPixmap] = None
        self._current_preview_pixmap: Optional[QPixmap] = None
        self._current_adjustments: dict[str, float | bool] = {}
        self._color_stats: ColorStats | None = None

        self._thread_pool = QThreadPool.globalInstance()
        self._preview_job_id = 0
        self._active_preview_workers: set[PreviewRenderWorker] = set()
        self._pending_session_disposals: list[PreviewSession] = []

        self._preview_update_timer = QTimer(self)
        self._preview_update_timer.setSingleShot(True)
        self._preview_update_timer.setInterval(50)
        self._preview_update_timer.timeout.connect(self._start_preview_job)

    # ------------------------------------------------------------------
    # Public API used by :class:`EditController`
    # ------------------------------------------------------------------
    def start_session(
        self,
        image: QImage,
        adjustments: Mapping[str, float],
        *,
        scale_for_viewport: bool = True,
    ) -> QPixmap:
        """Initialise a rendering session for *image* using *adjustments*."""

        self._cancel_pending_previews()
        prepared = self._prepare_preview_image(image, scale_for_viewport=scale_for_viewport)

        previous_session = self._preview_session
        if prepared.isNull():
            # The controller already guards against null images, however leaving
            # the safety net here prevents crashes should future call sites
            # forget to perform validation.
            self._preview_session = None
            self._base_image = None
            self._base_pixmap = None
            self._current_preview_pixmap = None
            if previous_session is not None:
                self._queue_session_for_disposal(previous_session)
                self._dispose_retired_sessions()
            self.image_cleared.emit()
            return QPixmap()

        try:
            self._preview_session = self._preview_backend.create_session(prepared)
        except RuntimeError as exc:
            # Hardware accelerated backends can occasionally fail on systems
            # with flaky OpenGL drivers.  Logging and retrying with a downgraded
            # backend keeps the edit experience functional instead of surfacing
            # a crash to the user.
            _LOGGER.warning(
                "Preview backend %s failed to create a session: %s",
                self._preview_backend.tier_name,
                exc,
            )
            replacement = fallback_preview_backend(self._preview_backend)
            if replacement is self._preview_backend:
                raise
            self._preview_backend = replacement
            _LOGGER.info(
                "Retrying preview session creation with %s backend",
                self._preview_backend.tier_name,
            )
            self._preview_session = self._preview_backend.create_session(prepared)
        self._base_image = QImage(prepared)
        base_pixmap = QPixmap.fromImage(prepared)
        self._base_pixmap = base_pixmap if not base_pixmap.isNull() else None
        self._current_preview_pixmap = self._base_pixmap
        self._current_adjustments = dict(adjustments)
        try:
            self._color_stats = compute_color_statistics(prepared)
        except Exception:
            # Defensive: computing color statistics can touch low-level image
            # buffers and may fail for malformed or exotic image formats. Log
            # the error and fall back to a safe default so the edit flow remains
            # usable.
            _LOGGER.exception("Failed to compute color statistics for preview image; using defaults")
            self._color_stats = None

        if previous_session is not None:
            self._queue_session_for_disposal(previous_session)
            self._dispose_retired_sessions()

        # Emit the unadjusted frame immediately so the caller can display an
        # instant response while the heavy lifting runs in the background.
        if self._base_pixmap is not None:
            self.preview_updated.emit(self._base_pixmap)
        else:
            self.image_cleared.emit()

        self._start_preview_job()
        return base_pixmap

    def stop_session(self) -> None:
        """Cancel outstanding work and release the active preview session."""

        self._cancel_pending_previews()
        if self._preview_session is not None:
            self._queue_session_for_disposal(self._preview_session)
            self._preview_session = None
            self._dispose_retired_sessions()
        self._base_image = None
        self._base_pixmap = None
        self._current_preview_pixmap = None
        self._current_adjustments.clear()
        self._color_stats = None

    def cancel_pending_updates(self) -> None:
        """Stop timers and invalidate in-flight previews without tearing down the session."""

        self._cancel_pending_previews()

    def update_adjustments(self, adjustments: Mapping[str, float | bool]) -> None:
        """Schedule a new preview render using *adjustments*."""

        self._current_adjustments = dict(adjustments)
        if self._preview_session is None:
            self.image_cleared.emit()
            return

        if self._preview_backend.supports_realtime:
            self._start_preview_job()
            return

        self._preview_update_timer.stop()
        self._preview_update_timer.start()

    # ------------------------------------------------------------------
    def resolve_adjustments(self, session_values: Mapping[str, float | bool]) -> dict[str, float]:
        """Return the shader-friendly adjustment mapping derived from *session_values*.

        The edit controller uses this helper to update the OpenGL viewer immediately after
        sliders change.  Reusing the preview manager's resolver guarantees that the GPU path
        applies the same Photos-compatible colour math as the CPU preview renderer.  Keeping the
        two surfaces perfectly in sync avoids duplicating transformation logic across modules.
        """

        return resolve_adjustment_mapping(session_values, stats=self._color_stats)

    def get_base_image_pixmap(self) -> Optional[QPixmap]:
        """Return the unadjusted pixmap currently backing the preview surface."""

        return self._base_pixmap

    def get_current_preview_pixmap(self) -> Optional[QPixmap]:
        """Return the latest adjusted preview pixmap."""

        return self._current_preview_pixmap

    def get_base_image(self) -> Optional[QImage]:
        """Expose the QImage currently used as the base render target."""

        return self._base_image

    def generate_scaled_neutral_preview(self, target_size: QSize) -> QImage:
        """Render a neutral GPU-scaled preview suitable for sidebar statistics.

        The helper delegates to :class:`GLImageViewer` so the scaling happens
        entirely on the GPU using the exact shader and sampling pipeline that is
        employed for onscreen rendering.  The returned image is always
        ``Format_ARGB32`` to ensure the sidebar worker can compute colour
        statistics without incurring additional conversions.
        """

        if self._viewer is None:
            _LOGGER.warning("generate_scaled_neutral_preview: viewer was not initialised")
            return QImage()

        neutral_adjustments = resolve_adjustment_mapping({}, stats=self._color_stats)
        try:
            return self._viewer.render_offscreen_image(target_size, neutral_adjustments)
        except Exception:
            _LOGGER.exception("generate_scaled_neutral_preview: GPU render failed")
            return QImage()

    # ------------------------------------------------------------------
    # Internal helpers mirrored from the legacy controller implementation
    # ------------------------------------------------------------------
    def _prepare_preview_image(
        self,
        image: QImage,
        *,
        scale_for_viewport: bool,
    ) -> QImage:
        """Return an image optimised for preview rendering throughput."""

        if not scale_for_viewport:
            return QImage(image)

        viewport_size = None

        # ``ImageViewer`` exposes its scroll area viewport for external event
        # filters.  Reusing that helper yields the exact drawable surface size
        # when the widget has already been laid out.
        if hasattr(self._viewer, "viewport_widget"):
            try:
                viewport = self._viewer.viewport_widget()
            except Exception:
                viewport = None
            if viewport is not None:
                size = viewport.size()
                if size.isValid() and not size.isEmpty():
                    viewport_size = size

        if viewport_size is None:
            size = self._viewer.size()
            if size.isValid() and not size.isEmpty():
                viewport_size = size

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

    def _start_preview_job(self) -> None:
        """Queue a background task that recalculates the preview image."""

        if self._preview_session is None:
            self.image_cleared.emit()
            return

        self._preview_job_id += 1
        job_id = self._preview_job_id

        final_adjustments = resolve_adjustment_mapping(
            self._current_adjustments,
            stats=self._color_stats,
        )

        worker = PreviewRenderWorker(
            self._preview_backend,
            self._preview_session,
            final_adjustments,
            job_id,
        )
        self._active_preview_workers.add(worker)

        worker.signals.finished.connect(self._on_preview_ready)

        def _handle_worker_finished(_image: QImage, _finished_job: int, *, worker_ref=worker) -> None:
            self._active_preview_workers.discard(worker_ref)
            self._dispose_retired_sessions()

        worker.signals.finished.connect(_handle_worker_finished)
        self._thread_pool.start(worker)

    def color_stats(self) -> ColorStats | None:
        """Expose the most recent colour statistics for reuse in the GL path."""

        return self._color_stats

    def _on_preview_ready(self, image: QImage, job_id: int) -> None:
        """Update the preview if the emitted job matches the latest request."""

        if job_id != self._preview_job_id:
            return

        if image.isNull():
            self._current_preview_pixmap = None
            self.image_cleared.emit()
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._current_preview_pixmap = None
            self.image_cleared.emit()
            return

        self._current_preview_pixmap = pixmap
        self.preview_updated.emit(pixmap)

    def _cancel_pending_previews(self) -> None:
        """Stop timers and invalidate outstanding preview work."""

        self._preview_update_timer.stop()
        self._preview_job_id += 1

    def _dispose_retired_sessions(self) -> None:
        """Dispose backend sessions queued for cleanup when safe."""

        if not self._pending_session_disposals:
            return

        active_sessions = [
            worker.session
            for worker in self._active_preview_workers
            if worker.session is not None
        ]
        for session in list(self._pending_session_disposals):
            if any(active is session for active in active_sessions):
                continue
            try:
                self._preview_backend.dispose_session(session)
            except Exception:
                pass
            try:
                self._pending_session_disposals.remove(session)
            except ValueError:
                continue

    def _queue_session_for_disposal(self, session: PreviewSession) -> None:
        """Record a session for deferred disposal if it is not already queued."""

        if session is None:
            return
        if any(queued is session for queued in self._pending_session_disposals):
            return
        self._pending_session_disposals.append(session)
