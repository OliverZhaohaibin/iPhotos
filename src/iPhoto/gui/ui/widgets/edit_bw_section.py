"""Black & White adjustment section for the edit sidebar."""

from __future__ import annotations

import logging
from functools import partial
from typing import Dict, Optional

from PySide6.QtCore import QThreadPool, Signal, Slot
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ....core.image_filters import apply_adjustments
from ..models.edit_session import EditSession
from ..tasks.thumbnail_generator_worker import ThumbnailGeneratorWorker
from .edit_strip import BWSlider
from .thumbnail_strip_slider import ThumbnailStripSlider

_LOGGER = logging.getLogger(__name__)


class EditBWSection(QWidget):
    """Expose the GPU-only black & white adjustments as a set of sliders."""

    adjustmentChanged = Signal(str, float)
    """Emitted when one of the sliders modifies its backing adjustment."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[EditSession] = None
        self._sliders: Dict[str, BWSlider] = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._active_thumbnail_workers: list[ThumbnailGeneratorWorker] = []
        self._last_master_value = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.master_slider = ThumbnailStripSlider(
            "Black & White",
            self,
            minimum=0.0,
            maximum=1.0,
            initial=0.0,
        )
        self.master_slider.set_preview_generator(self._generate_master_preview)
        self.master_slider.valueChanged.connect(self._handle_master_slider_changed)
        self.master_slider.clickedWhenDisabled.connect(self._handle_disabled_slider_click)
        layout.addWidget(self.master_slider)

        slider_specs = [
            ("Intensity", "BW_Intensity", 0.0, 1.0),
            ("Neutrals", "BW_Neutrals", -1.0, 1.0),
            ("Tone", "BW_Tone", -1.0, 1.0),
            ("Grain", "BW_Grain", 0.0, 1.0),
        ]

        for label, key, minimum, maximum in slider_specs:
            slider = BWSlider(label, self, minimum=minimum, maximum=maximum, initial=0.0)
            slider.valueChanged.connect(lambda value, k=key: self._handle_slider_changed(k, value))
            layout.addWidget(slider)
            self._sliders[key] = slider

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

    def refresh_from_session(self) -> None:
        """Synchronise the slider positions with the active session state."""

        if self._session is None:
            self._reset_slider_values()
            self._apply_enabled_state(False)
            self.master_slider.update_from_value(0.0)
            return

        enabled = bool(self._session.value("BW_Enabled"))
        self._apply_enabled_state(enabled)

        intensity = float(self._session.value("BW_Intensity"))
        neutrals = float(self._session.value("BW_Neutrals"))
        tone = float(self._session.value("BW_Tone"))

        for key, slider in self._sliders.items():
            value = float(self._session.value(key))
            slider.blockSignals(True)
            slider.setValue(value, emit=False)
            slider.blockSignals(False)
            slider.setEnabled(enabled)

        estimated_master = self._estimate_master_value(intensity, neutrals, tone)
        self._last_master_value = estimated_master
        self.master_slider.blockSignals(True)
        self.master_slider.setEnabled(enabled)
        self.master_slider.update_from_value(estimated_master if enabled else 0.0)
        self.master_slider.blockSignals(False)
        self._sliders["BW_Grain"].setEnabled(enabled)

    def set_preview_image(self, image) -> None:
        """Forward *image* to the master slider so it can refresh thumbnails."""

        self.master_slider.setImage(image)
        self._start_master_thumbnail_generation()

    # ------------------------------------------------------------------
    def _reset_slider_values(self) -> None:
        for slider in self._sliders.values():
            slider.blockSignals(True)
            slider.setValue(0.0, emit=False)
            slider.blockSignals(False)

    def _apply_enabled_state(self, enabled: bool) -> None:
        self.master_slider.setEnabled(enabled)
        for slider in self._sliders.values():
            slider.setEnabled(enabled)

    def _estimate_master_value(self, intensity: float, neutrals: float, tone: float) -> float:
        """Return the master slider value that best reproduces the current params."""

        # Recover the master slider position only when the stored parameters match the aggregate
        # curve.  Otherwise display ``0`` so the UI signals that adjustments were made manually.
        candidate = 0.0
        if intensity > 1e-6:
            candidate = self._invert_smooth01(max(0.0, min(1.0, intensity)))
        aggregate = self._aggregate_curve(candidate)
        if (
            abs(aggregate["Intensity"] - intensity) < 1e-3
            and abs(aggregate["Neutrals"] - neutrals) < 1e-3
            and abs(aggregate["Tone"] - tone) < 1e-3
        ):
            return candidate
        return 0.0

    def _invert_smooth01(self, value: float) -> float:
        value = max(0.0, min(1.0, value))
        low, high = 0.0, 1.0
        for _ in range(16):
            mid = (low + high) / 2.0
            if self._smooth01(mid) < value:
                low = mid
            else:
                high = mid
        return (low + high) / 2.0

    # ------------------------------------------------------------------
    @Slot(str, object)
    def _on_session_value_changed(self, key: str, value: object) -> None:
        if key == "BW_Enabled":
            self._apply_enabled_state(bool(value))
            return
        if key not in self._sliders or self._session is None:
            return
        slider = self._sliders[key]
        slider.blockSignals(True)
        slider.setValue(float(value), emit=False)
        slider.blockSignals(False)
        if key != "BW_Grain":
            self._last_master_value = self._estimate_master_value(
                float(self._session.value("BW_Intensity")),
                float(self._session.value("BW_Neutrals")),
                float(self._session.value("BW_Tone")),
            )
            self.master_slider.blockSignals(True)
            self.master_slider.update_from_value(self._last_master_value)
            self.master_slider.blockSignals(False)

    @Slot()
    def _on_session_reset(self) -> None:
        self.refresh_from_session()

    def _handle_master_slider_changed(self, value: float) -> None:
        if self._session is None:
            return
        aggregate = self._aggregate_curve(value)
        updates = {
            "BW_Intensity": aggregate["Intensity"],
            "BW_Neutrals": aggregate["Neutrals"],
            "BW_Tone": aggregate["Tone"],
        }
        self._session.set_values(updates)
        if not self._session.value("BW_Enabled"):
            self._session.set_value("BW_Enabled", True)
        self._last_master_value = float(value)

    def _handle_slider_changed(self, key: str, new_value: float) -> None:
        if self._session is None:
            return
        self._session.set_value(key, new_value)
        self.adjustmentChanged.emit(key, new_value)
        if key != "BW_Grain":
            self._last_master_value = 0.0
            self.master_slider.blockSignals(True)
            self.master_slider.update_from_value(0.0)
            self.master_slider.blockSignals(False)

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

    # ------------------------------------------------------------------
    def _smooth01(self, value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    def _aggregate_curve(self, master: float) -> dict[str, float]:
        master = max(0.0, min(1.0, master))
        intensity = self._smooth01(master)
        neutrals = max(-1.0, min(1.0, 0.25 * (2.0 * master - 1.0)))
        tone = max(-1.0, min(1.0, -0.10 + 0.60 * master))
        return {
            "Intensity": intensity,
            "Neutrals": neutrals,
            "Tone": tone,
        }

    def _generate_master_preview(self, image, value: float):
        curve = self._aggregate_curve(value)
        adjustments = {
            "BW_Enabled": True,
            "BW_Intensity": curve["Intensity"],
            "BW_Neutrals": curve["Neutrals"],
            "BW_Tone": curve["Tone"],
            "BW_Grain": 0.0,
        }
        return apply_adjustments(image, adjustments)

