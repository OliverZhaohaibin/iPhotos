"""Black & White adjustment section for the edit sidebar."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Dict, Optional

from PySide6.QtCore import QThreadPool, Signal, Slot
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ....core.bw_resolver import BWParams, apply_bw_preview, resolve_effective_params
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
            initial=0.5,
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
            _SliderSpec("Neutrals", "BW_Neutrals", 0.0, 1.0),
            _SliderSpec("Tone", "BW_Tone", 0.0, 1.0),
            _SliderSpec("Grain", "BW_Grain", 0.0, 1.0),
        ]
        initial_values = {
            "BW_Intensity": 0.5,
            "BW_Neutrals": 0.0,
            "BW_Tone": 0.0,
            "BW_Grain": 0.0,
        }
        for spec in specs:
            slider = BWSlider(
                spec.label,
                self,
                minimum=spec.minimum,
                maximum=spec.maximum,
                initial=initial_values.get(spec.key, 0.0),
            )
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
            self.master_slider.update_from_value(0.5)
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
        self._updating_ui = True
        try:
            self._apply_enabled_state(enabled)
            self.master_slider.setEnabled(enabled)
            self.master_slider.update_from_value(params.master if enabled else 0.5)
            for key, slider in self._sliders.items():
                slider.setEnabled(enabled)
            self._sliders["BW_Intensity"].setValue(params.intensity, emit=False)
            self._sliders["BW_Neutrals"].setValue(params.neutrals, emit=False)
            self._sliders["BW_Tone"].setValue(params.tone, emit=False)
            self._sliders["BW_Grain"].setValue(params.grain, emit=False)
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
            self.master_slider.setValue(0.5, emit=False)
            self._sliders["BW_Intensity"].setValue(0.5, emit=False)
            self._sliders["BW_Neutrals"].setValue(0.0, emit=False)
            self._sliders["BW_Tone"].setValue(0.0, emit=False)
            self._sliders["BW_Grain"].setValue(0.0, emit=False)
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

    def _gather_user_params(self) -> BWParams:
        """Return the user supplied slider values as a :class:`BWParams` bundle."""

        return BWParams(
            master=self.master_slider.value(),
            intensity=self._sliders["BW_Intensity"].value(),
            neutrals=self._sliders["BW_Neutrals"].value(),
            tone=self._sliders["BW_Tone"].value(),
            grain=self._sliders["BW_Grain"].value(),
        )

    def _emit_preview_params(self) -> None:
        """Compute the effective parameters and emit the preview signal."""

        if self._updating_ui:
            return
        user_params = self._gather_user_params()
        effective = resolve_effective_params(user_params.master, user_params)
        self.paramsPreviewed.emit(effective)

    def _emit_commit_params(self) -> None:
        """Compute the effective parameters and emit the commit signal."""

        user_params = self._gather_user_params()
        effective = resolve_effective_params(user_params.master, user_params)
        self.paramsCommitted.emit(effective)

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
        self._emit_preview_params()

    def _handle_master_slider_committed(self, value: float) -> None:
        if self._session is not None:
            self._session.set_value("BW_Master", value)
        self._emit_commit_params()

    def _handle_slider_changed(self, key: str, _value: float) -> None:
        if self._updating_ui:
            return
        self._emit_preview_params()

    def _handle_slider_committed(self, key: str, value: float) -> None:
        if self._session is not None:
            self._session.set_value(key, value)
        self._emit_commit_params()
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
        user_params = self._gather_user_params()
        effective = resolve_effective_params(value, user_params)
        return apply_bw_preview(image, effective)

__all__ = ["EditBWSection"]
