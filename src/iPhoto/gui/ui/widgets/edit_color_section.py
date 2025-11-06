"""Color adjustment section used inside the edit sidebar."""

from __future__ import annotations

import logging
from functools import partial
from typing import Dict, Optional

from PySide6.QtCore import QThreadPool, Signal, Slot, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QVBoxLayout,
    QWidget,
    QGraphicsOpacityEffect,
)

from ....core.color_resolver import COLOR_KEYS, COLOR_RANGES, ColorResolver, ColorStats
from ....core.color_resolver import compute_color_statistics
from ....core.image_filters import apply_adjustments
from ..models.edit_session import EditSession
from .collapsible_section import CollapsibleSection
from .edit_strip import BWSlider
from .thumbnail_strip_slider import ThumbnailStripSlider
from ..tasks.thumbnail_generator_worker import ThumbnailGeneratorWorker


_LOGGER = logging.getLogger(__name__)


class EditColorSection(QWidget):
    """Container widget hosting the "Color" adjustment sliders."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[EditSession] = None
        self._rows: Dict[str, _SliderRow] = {}
        self._color_stats: ColorStats = ColorStats()
        self._thread_pool = QThreadPool.globalInstance()
        self._active_thumbnail_workers: list[ThumbnailGeneratorWorker] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.master_slider = ThumbnailStripSlider(
            None,
            self,
            minimum=-1.0,
            maximum=1.0,
            initial=0.0,
        )
        self.master_slider.set_preview_generator(self._generate_master_preview)
        self.master_slider.valueChanged.connect(self._handle_master_slider_changed)
        self.master_slider.clickedWhenDisabled.connect(self._handle_disabled_slider_click)
        # Offload thumbnail rendering to the shared thread pool so dragging the
        # slider never blocks the GUI thread.
        self.master_slider.set_thumbnail_job_factory(self._queue_master_thumbnail_generation)
        layout.addWidget(self.master_slider)

        options_container = QFrame(self)
        options_container.setFrameShape(QFrame.Shape.NoFrame)
        options_container.setFrameShadow(QFrame.Shadow.Plain)
        options_layout = QVBoxLayout(options_container)
        options_layout.setContentsMargins(12, 12, 12, 12)
        options_layout.setSpacing(1)

        labels = [
            ("Saturation", "Saturation"),
            ("Vibrance", "Vibrance"),
            ("Cast", "Cast"),
        ]
        for label_text, key in labels:
            minimum, maximum = COLOR_RANGES[key]
            row = _SliderRow(key, label_text, minimum, maximum, parent=options_container)
            row.uiValueChanged.connect(self._handle_sub_slider_changed)
            row.clickedWhenDisabled.connect(self._handle_disabled_slider_click)
            options_layout.addWidget(row)
            self._rows[key] = row

        self.options_section = CollapsibleSection(
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
        """Associate the section with *session* and refresh slider state."""

        if self._session is session:
            return
        if self._session is not None:
            self._session.valueChanged.disconnect(self._on_session_value_changed)
            self._session.resetPerformed.disconnect(self._on_session_reset)
        self._session = session

        for row in self._rows.values():
            row.setSession(session)

        if session is not None:
            session.valueChanged.connect(self._on_session_value_changed)
            session.resetPerformed.connect(self._on_session_reset)
            stats = session.color_stats()
            if stats is not None:
                self._color_stats = stats
            self.refresh_from_session()
        else:
            self._disable_rows()
            self.master_slider.setEnabled(False)
            self.master_slider.update_from_value(0.0)

    def refresh_from_session(self) -> None:
        """Synchronise slider positions with the attached session."""

        if self._session is None:
            self._disable_rows()
            self.master_slider.setEnabled(False)
            self.master_slider.update_from_value(0.0)
            return
        master_value = float(self._session.value("Color_Master"))
        self.master_slider.update_from_value(master_value)
        enabled = bool(self._session.value("Color_Enabled"))
        self.master_slider.setEnabled(enabled)
        self._apply_enabled_state(enabled)
        self._update_all_sub_sliders_ui()
        for row in self._rows.values():
            row.setEnabled(enabled)

    def _disable_rows(self) -> None:
        for row in self._rows.values():
            row.setEnabled(False)
            row.update_from_value(0.0)

    # ------------------------------------------------------------------
    def _on_session_value_changed(self, key: str, value: float | bool) -> None:
        if key == "Color_Enabled":
            self._apply_enabled_state(bool(value))
            return

        if key == "Color_Master":
            self.master_slider.update_from_value(float(value))
            self._update_all_sub_sliders_ui()
            return

        if key in COLOR_KEYS:
            self._update_all_sub_sliders_ui()

    def _on_session_reset(self) -> None:
        self.refresh_from_session()

    def _handle_master_slider_changed(self, new_value: float) -> None:
        if self._session is None:
            return
        self._session.set_value("Color_Master", float(new_value))

    @Slot(str, float)
    def _handle_sub_slider_changed(self, key: str, new_ui_value: float) -> None:
        """Persist the delta for *key* after the user moves a fine-tuning slider."""

        if self._session is None:
            return

        master_value = float(self._session.value("Color_Master"))
        base_values = ColorResolver.distribute_master(master_value, self._color_stats)
        base_value = float(base_values.get(key, 0.0))

        delta_value = _clamp(new_ui_value - base_value, -1.0, 1.0)
        self._session.set_value(key, delta_value)

    def _update_all_sub_sliders_ui(self) -> None:
        """Recompute and display the final Color values for every fine-tuning slider."""

        if self._session is None:
            return

        master_value = float(self._session.value("Color_Master"))
        base_values = ColorResolver.distribute_master(master_value, self._color_stats)

        for key in COLOR_KEYS:
            row = self._rows.get(key)
            if row is None:
                continue

            base_value = float(base_values.get(key, 0.0))
            delta_value = float(self._session.value(key))
            minimum, maximum = COLOR_RANGES[key]
            final_value = _clamp(base_value + delta_value, minimum, maximum)

            row.update_from_value(final_value)

    def _apply_enabled_state(self, enabled: bool) -> None:
        self.master_slider.setEnabled(enabled)
        for row in self._rows.values():
            row.setEnabled(enabled)

    def set_preview_image(self, image) -> None:
        """Forward *image* to the master slider and refresh cached statistics."""

        if image is not None:
            stats = compute_color_statistics(image)
            self._color_stats = stats
            if self._session is not None:
                self._session.set_color_stats(stats)
        self.master_slider.setImage(image)

    # ------------------------------------------------------------------
    def _queue_master_thumbnail_generation(
        self, slider: ThumbnailStripSlider, generation: int
    ) -> None:
        """Submit a worker that regenerates the master slider thumbnails."""

        image = slider.base_image()
        if image is None:
            return

        values = slider.tick_values()
        if not values:
            return

        worker = ThumbnailGeneratorWorker(
            image,
            values,
            slider.preview_generator(),
            target_height=slider.track_height(),
            generation_id=generation,
        )

        worker.signals.thumbnail_ready.connect(
            slider.update_thumbnail,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.signals.error.connect(partial(self._on_thumbnail_error, worker))
        worker.signals.finished.connect(partial(self._on_thumbnail_finished, worker))

        self._active_thumbnail_workers.append(worker)
        self._thread_pool.start(worker)

    @Slot(int, str)
    def _on_thumbnail_error(self, worker: ThumbnailGeneratorWorker, generation_id: int, message: str) -> None:
        """Record worker errors to aid diagnosing preview generation issues."""

        del generation_id
        if worker in self._active_thumbnail_workers:
            _LOGGER.error("Color thumbnail generation failed: %s", message)

    @Slot(int)
    def _on_thumbnail_finished(self, worker: ThumbnailGeneratorWorker, generation_id: int) -> None:
        """Drop finished workers so repeated loads do not leak references."""

        del generation_id
        try:
            self._active_thumbnail_workers.remove(worker)
        except ValueError:
            pass

    @Slot()
    def _handle_disabled_slider_click(self) -> None:
        """Re-enables the Color adjustments if a disabled slider is clicked."""

        if self._session is not None and not self._session.value("Color_Enabled"):
            self._session.set_value("Color_Enabled", True)

    def _generate_master_preview(self, image: QImage, value: float) -> QImage:
        """Return a preview frame illustrating the Color master slider effect."""

        stats = compute_color_statistics(image)
        resolved = ColorResolver.resolve_color_vector(value, None, stats=stats)
        gain_r, gain_g, gain_b = stats.white_balance_gain
        adjustments = {
            "Light_Enabled": False,
            "Color_Enabled": True,
            "Saturation": resolved.get("Saturation", 0.0),
            "Vibrance": resolved.get("Vibrance", 0.0),
            "Cast": resolved.get("Cast", 0.0),
            "Color_Gain_R": gain_r,
            "Color_Gain_G": gain_g,
            "Color_Gain_B": gain_b,
        }
        return apply_adjustments(image, adjustments, color_stats=stats)


class _SliderRow(QFrame):
    """Helper widget bundling a label, slider and numeric read-out."""

    uiValueChanged = Signal(str, float)
    """Emitted whenever the slider's visual value changes due to user interaction."""

    clickedWhenDisabled = Signal()

    def __init__(
        self,
        key: str,
        label: str,
        minimum: float,
        maximum: float,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._key = key
        self._session: Optional[EditSession] = None

        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.slider = BWSlider(label, self, minimum=minimum, maximum=maximum, initial=0.0)
        layout.addWidget(self.slider)
        self.slider.valueChanged.connect(self._handle_slider_changed)

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)

    def setSession(self, session: Optional[EditSession]) -> None:
        self._session = session

    def setEnabled(self, enabled: bool) -> None:  # type: ignore[override]
        """Keep the row enabled to capture clicks, but disable the visual slider."""

        super().setEnabled(True)
        self.slider.setEnabled(enabled)
        self._opacity_effect.setOpacity(1.0 if enabled else 0.5)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        """Handle clicks when the slider is disabled to re-enable it."""

        if event.button() == Qt.MouseButton.LeftButton:
            if (
                not self.slider.isEnabled()
                and self.slider.geometry().contains(event.position().toPoint())
            ):
                self.clickedWhenDisabled.emit()

                slider_event = QMouseEvent(
                    event.type(),
                    self.slider.mapFrom(self, event.position().toPoint()),
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                QApplication.sendEvent(self.slider, slider_event)
                event.accept()
                return
        super().mousePressEvent(event)

    def update_from_value(self, value: float) -> None:
        block = self.slider.blockSignals(True)
        try:
            self.slider.setValue(value, emit=False)
        finally:
            self.slider.blockSignals(block)

    # ------------------------------------------------------------------
    def _handle_slider_changed(self, new_value: float) -> None:
        """Relay the updated slider value while tagging it with the adjustment *key*."""

        self.uiValueChanged.emit(self._key, float(new_value))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value

