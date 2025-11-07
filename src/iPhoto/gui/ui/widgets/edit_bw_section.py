"""Black & White adjustment section for the edit sidebar."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Dict, Optional

from PySide6.QtCore import QThreadPool, Signal, Slot
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ....core.bw_resolver import BWParams, apply_bw_preview, params_from_master
from ....core.bw_resolver import smooth01  # re-exported for inversion helpers
from ..models.edit_session import EditSession
from ..tasks.thumbnail_generator_worker import ThumbnailGeneratorWorker
from .collapsible_section import CollapsibleSubSection
from .edit_strip import BWSlider
from .thumbnail_strip_slider import ThumbnailStripSlider

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SliderSpec:
    label: str
    key: str
    minimum: float
    maximum: float


class EditBWSection(QWidget):
    """Expose the GPU-only black & white adjustments as a set of sliders."""

    adjustmentChanged = Signal(str, float)
    """Emitted when a slider commits a new value to the session."""

    paramsPreviewed = Signal(BWParams)
    """Emitted while the user drags a control so the viewer can update live."""

    paramsCommitted = Signal(BWParams)
    """Emitted once the interaction ends and the session should persist the change."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[EditSession] = None
        self._sliders: Dict[str, BWSlider] = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._active_thumbnail_workers: list[ThumbnailGeneratorWorker] = []
        self._updating_ui = False

        # Match the surrounding light/color sections so separators and padding align.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.master_slider = ThumbnailStripSlider(
            None,
            self,
            minimum=0.0,
            maximum=1.0,
            initial=0.0,
        )
        self.master_slider.set_preview_generator(self._generate_master_preview)
        self.master_slider.valueChanged.connect(self._handle_master_slider_changed)
        self.master_slider.valueCommitted.connect(self._handle_master_slider_committed)
        self.master_slider.clickedWhenDisabled.connect(self._handle_disabled_slider_click)
        layout.addWidget(self.master_slider)

        options_container = QFrame(self)
        options_container.setFrameShape(QFrame.Shape.NoFrame)
        options_container.setFrameShadow(QFrame.Shadow.Plain)
        # Keep the manual sliders indented by 12px so they line up with peers.
        options_layout = QVBoxLayout(options_container)
        options_layout.setContentsMargins(12, 12, 12, 12)
        options_layout.setSpacing(6)

        specs = [
            _SliderSpec("Intensity", "BW_Intensity", 0.0, 1.0),
            _SliderSpec("Neutrals", "BW_Neutrals", -1.0, 1.0),
            _SliderSpec("Tone", "BW_Tone", -1.0, 1.0),
            _SliderSpec("Grain", "BW_Grain", 0.0, 1.0),
        ]
        for spec in specs:
            slider = BWSlider(spec.label, self, minimum=spec.minimum, maximum=spec.maximum, initial=0.0)
            slider.valueChanged.connect(partial(self._handle_slider_changed, spec.key))
            slider.valueCommitted.connect(partial(self._handle_slider_committed, spec.key))
            options_layout.addWidget(slider)
            self._sliders[spec.key] = slider

        self.options_section = CollapsibleSubSection(
            "Options",
            "slider.horizontal.3.svg",
            options_container,
            self,
        )
        self.options_section.set_expanded(False)
        layout.addWidget(self.options_section)
        layout.addStretch(1)

    # ------------------------------------------------------------------
    def bind_session(self, session: Optional[EditSession]) -> None:
        """Attach *session* so slider updates are persisted and reflected."""

        if self._session is session:
            return

        if self._session is not None:
            try:
                self._session.valueChanged.disconnect(self._on_session_value_changed)
            except (TypeError, RuntimeError):
                pass
            try:
                self._session.resetPerformed.disconnect(self._on_session_reset)
            except (TypeError, RuntimeError):
                pass

        self._session = session

        if session is not None:
            session.valueChanged.connect(self._on_session_value_changed)
            session.resetPerformed.connect(self._on_session_reset)
            self.refresh_from_session()
        else:
            self._reset_slider_values()
            self._apply_enabled_state(False)
            self.master_slider.update_from_value(0.0)
            self.options_section.set_expanded(False)

    def refresh_from_session(self) -> None:
        """Synchronise the slider positions with the active session state."""

        if self._session is None:
            self._reset_slider_values()
            self._apply_enabled_state(False)
            self.master_slider.update_from_value(0.0)
            self.options_section.set_expanded(False)
            return

        enabled = bool(self._session.value("BW_Enabled"))
        params = self._session_params()
        master_value = self._resolve_master_value(params)
        self._updating_ui = True
        try:
            self._apply_enabled_state(enabled)
            self.master_slider.setEnabled(enabled)
            self.master_slider.update_from_value(master_value if enabled else 0.0)
            for key, slider in self._sliders.items():
                slider.setEnabled(enabled)
            self._apply_params_to_sliders(params)
        finally:
            self._updating_ui = False

    def set_preview_image(self, image) -> None:
        """Forward *image* to the master slider so it can refresh thumbnails."""

        self.master_slider.setImage(image)
        self._start_master_thumbnail_generation()

    # ------------------------------------------------------------------
    def _reset_slider_values(self) -> None:
        self._updating_ui = True
        try:
            for slider in self._sliders.values():
                slider.setValue(0.0, emit=False)
        finally:
            self._updating_ui = False

    def _apply_enabled_state(self, enabled: bool) -> None:
        self.master_slider.setEnabled(enabled)
        for slider in self._sliders.values():
            slider.setEnabled(enabled)
        if not enabled:
            self.options_section.set_expanded(False)

    def _session_params(self) -> BWParams:
        if self._session is None:
            return BWParams()
        return BWParams(
            intensity=float(self._session.value("BW_Intensity")),
            neutrals=float(self._session.value("BW_Neutrals")),
            tone=float(self._session.value("BW_Tone")),
            grain=float(self._session.value("BW_Grain")),
            master=float(self._session.value("BW_Master")),
        )

    def _apply_params_to_sliders(self, params: BWParams) -> None:
        for key, slider in self._sliders.items():
            if key == "BW_Intensity":
                slider.setValue(params.intensity, emit=False)
            elif key == "BW_Neutrals":
                slider.setValue(params.neutrals, emit=False)
            elif key == "BW_Tone":
                slider.setValue(params.tone, emit=False)
            elif key == "BW_Grain":
                slider.setValue(params.grain, emit=False)

    def _resolve_master_value(self, params: BWParams) -> float:
        if params.master > 1e-6:
            derived = params_from_master(params.master, grain=params.grain)
            if self._params_match(params, derived):
                return params.master
        if params.intensity <= 1e-6:
            return 0.0
        candidate = self._invert_smooth01(max(0.0, min(1.0, params.intensity)))
        derived = params_from_master(candidate, grain=params.grain)
        if self._params_match(params, derived):
            return candidate
        return 0.0

    def _params_match(self, lhs: BWParams, rhs: BWParams) -> bool:
        return (
            abs(lhs.intensity - rhs.intensity) < 1e-3
            and abs(lhs.neutrals - rhs.neutrals) < 1e-3
            and abs(lhs.tone - rhs.tone) < 1e-3
        )

    def _invert_smooth01(self, value: float) -> float:
        value = max(0.0, min(1.0, value))
        low, high = 0.0, 1.0
        for _ in range(16):
            mid = (low + high) / 2.0
            if smooth01(mid) < value:
                low = mid
            else:
                high = mid
        return (low + high) / 2.0

    def _gather_slider_params(self, *, master_override: Optional[float] = None) -> BWParams:
        intensity = self._sliders["BW_Intensity"].value()
        neutrals = self._sliders["BW_Neutrals"].value()
        tone = self._sliders["BW_Tone"].value()
        grain = self._sliders["BW_Grain"].value()
        master = master_override if master_override is not None else self.master_slider.value()
        return BWParams(
            intensity=intensity,
            neutrals=neutrals,
            tone=tone,
            grain=grain,
            master=master,
        )

    # ------------------------------------------------------------------
    @Slot(str, object)
    def _on_session_value_changed(self, key: str, _value: object) -> None:
        if key == "BW_Enabled":
            self._apply_enabled_state(bool(self._session.value("BW_Enabled")))
            return
        if key.startswith("BW_"):
            self.refresh_from_session()

    @Slot()
    def _on_session_reset(self) -> None:
        self.refresh_from_session()

    def _handle_master_slider_changed(self, value: float) -> None:
        if self._updating_ui:
            return
        params = params_from_master(value, grain=self._sliders["BW_Grain"].value())
        self._updating_ui = True
        try:
            self._apply_params_to_sliders(params)
        finally:
            self._updating_ui = False
        self.paramsPreviewed.emit(params)

    def _handle_master_slider_committed(self, value: float) -> None:
        derived = params_from_master(value, grain=self._sliders["BW_Grain"].value())
        # Rebuild the payload so the master value is persisted alongside the derived sliders.
        final_params = BWParams(
            intensity=derived.intensity,
            neutrals=derived.neutrals,
            tone=derived.tone,
            grain=derived.grain,
            master=value,
        )
        self.paramsCommitted.emit(final_params)

    def _handle_slider_changed(self, key: str, _value: float) -> None:
        if self._updating_ui:
            return
        self._updating_ui = True
        try:
            self.master_slider.blockSignals(True)
            self.master_slider.setValue(0.0)
            self.master_slider.blockSignals(False)
        finally:
            self._updating_ui = False
        params = self._gather_slider_params(master_override=0.0)
        self.paramsPreviewed.emit(params)

    def _handle_slider_committed(self, key: str, value: float) -> None:
        params = self._gather_slider_params(master_override=0.0)
        self.paramsCommitted.emit(params)
        self.adjustmentChanged.emit(key, value)

    @Slot()
    def _handle_disabled_slider_click(self) -> None:
        if self._session is not None and not self._session.value("BW_Enabled"):
            self._session.set_value("BW_Enabled", True)

    # ------------------------------------------------------------------
    def _start_master_thumbnail_generation(self) -> None:
        image = self.master_slider.base_image()
        if image is None:
            return
        values = self.master_slider.tick_values()
        if not values:
            return
        worker = ThumbnailGeneratorWorker(
            image,
            values,
            self._generate_master_preview,
            target_height=self.master_slider.track_height(),
            generation_id=self.master_slider.generation_id(),
        )
        worker.signals.thumbnail_ready.connect(self.master_slider.update_thumbnail)
        worker.signals.error.connect(partial(self._on_thumbnail_error, worker))
        worker.signals.finished.connect(partial(self._on_thumbnail_finished, worker))
        self._active_thumbnail_workers.append(worker)
        self._thread_pool.start(worker)

    def _on_thumbnail_error(self, worker: ThumbnailGeneratorWorker, generation_id: int, message: str) -> None:
        del generation_id
        if worker in self._active_thumbnail_workers:
            _LOGGER.error("Black & White thumbnail generation failed: %s", message)

    def _on_thumbnail_finished(self, worker: ThumbnailGeneratorWorker, generation_id: int) -> None:
        del generation_id
        try:
            self._active_thumbnail_workers.remove(worker)
        except ValueError:
            pass

    def _generate_master_preview(self, image, value: float):
        params = params_from_master(value, grain=self._sliders["BW_Grain"].value())
        return apply_bw_preview(image, params)

*** End of File
