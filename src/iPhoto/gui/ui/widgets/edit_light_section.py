"""Light adjustment section used inside the edit sidebar."""

from __future__ import annotations

import logging
from functools import partial
from typing import Dict, Optional


from PySide6.QtCore import QThreadPool, Signal, Slot, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QVBoxLayout,
    QWidget,
    QGraphicsOpacityEffect,
)

from ....core.light_resolver import LIGHT_KEYS, _clamp, resolve_light_vector
from ..models.edit_session import EditSession
from .collapsible_section import CollapsibleSection
from .edit_strip import BWSlider
from .thumbnail_strip_slider import ThumbnailStripSlider
from ..tasks.thumbnail_generator_worker import ThumbnailGeneratorWorker


_LOGGER = logging.getLogger(__name__)


class EditLightSection(QWidget):
    """Container widget hosting the "Light" adjustment sliders."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[EditSession] = None
        self._rows: Dict[str, _SliderRow] = {}
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
        self.master_slider.valueChanged.connect(self._handle_master_slider_changed)
        self.master_slider.clickedWhenDisabled.connect(self._handle_disabled_slider_click)
        layout.addWidget(self.master_slider)

        # Use a frameless ``QFrame`` so the collapsible section displays plain sliders
        # without inheriting the decorative border and title provided by ``QGroupBox``.
        options_container = QFrame(self)
        options_container.setFrameShape(QFrame.Shape.NoFrame)
        options_container.setFrameShadow(QFrame.Shadow.Plain)
        options_layout = QVBoxLayout(options_container)
        options_layout.setContentsMargins(12, 12, 12, 12)
        options_layout.setSpacing(1)

        labels = [
            ("Brilliance", "Brilliance"),
            ("Exposure", "Exposure"),
            ("Highlights", "Highlights"),
            ("Shadows", "Shadows"),
            ("Brightness", "Brightness"),
            ("Contrast", "Contrast"),
            ("Black Point", "BlackPoint"),
        ]
        for label_text, key in labels:
            row = _SliderRow(key, label_text, parent=options_container)
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
        master_value = float(self._session.value("Light_Master"))
        self.master_slider.update_from_value(master_value)
        enabled = bool(self._session.value("Light_Enabled"))
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
        if key == "Light_Enabled":
            self._apply_enabled_state(bool(value))
            return

        if key == "Light_Master":
            self.master_slider.update_from_value(float(value))
            self._update_all_sub_sliders_ui()
            return

        if key in LIGHT_KEYS:
            self._update_all_sub_sliders_ui()

    def _on_session_reset(self) -> None:
        self.refresh_from_session()

    def _handle_master_slider_changed(self, new_value: float) -> None:
        if self._session is None:
            return
        self._session.set_value("Light_Master", float(new_value))

    @Slot(str, float)
    def _handle_sub_slider_changed(self, key: str, new_ui_value: float) -> None:
        """Persist the delta for *key* after the user moves a fine-tuning slider."""

        if self._session is None:
            return

        master_value = float(self._session.value("Light_Master"))
        base_values = resolve_light_vector(master_value, None)
        base_value = float(base_values.get(key, 0.0))

        # The UI shows ``base + delta`` so we recover the delta component before persisting it.
        delta_value = _clamp(new_ui_value - base_value)
        self._session.set_value(key, delta_value)

    def _update_all_sub_sliders_ui(self) -> None:
        """Recompute and display the final Light values for every fine-tuning slider."""

        if self._session is None:
            return

        master_value = float(self._session.value("Light_Master"))
        base_values = resolve_light_vector(master_value, None)

        for key in LIGHT_KEYS:
            row = self._rows.get(key)
            if row is None:
                continue

            base_value = float(base_values.get(key, 0.0))
            delta_value = float(self._session.value(key))
            # Combine the resolved base with the stored delta to display the true applied value.
            final_value = _clamp(base_value + delta_value)

            row.update_from_value(final_value)

    def _apply_enabled_state(self, enabled: bool) -> None:
        self.master_slider.setEnabled(enabled)
        for row in self._rows.values():
            row.setEnabled(enabled)

    def set_preview_image(self, image) -> None:
        """Forward *image* to the master slider so it can refresh thumbnails."""

        self.master_slider.setImage(image)
        self._start_master_thumbnail_generation()

    @Slot()
    def _handle_disabled_slider_click(self) -> None:
        """Re-enables the Light adjustments if a disabled slider is clicked."""
        if self._session is not None and not self._session.value("Light_Enabled"):
            self._session.set_value("Light_Enabled", True)

    # ------------------------------------------------------------------
    def _start_master_thumbnail_generation(self) -> None:
        """Launch a background task that fills the master slider thumbnails."""

        image = self.master_slider.base_image()
        if image is None:
            return

        values = self.master_slider.tick_values()
        if not values:
            return

        worker = ThumbnailGeneratorWorker(
            image,
            values,
            self.master_slider.preview_generator(),
            target_height=self.master_slider.track_height(),
            generation_id=self.master_slider.generation_id(),
        )

        worker.signals.thumbnail_ready.connect(self.master_slider.update_thumbnail)
        worker.signals.error.connect(partial(self._on_thumbnail_error, worker))
        worker.signals.finished.connect(partial(self._on_thumbnail_finished, worker))

        self._active_thumbnail_workers.append(worker)
        self._thread_pool.start(worker)

    @Slot(int, str)
    def _on_thumbnail_error(self, worker: ThumbnailGeneratorWorker, generation_id: int, message: str) -> None:
        """Log thumbnail generation failures while keeping the worker alive."""

        del generation_id  # The slider ignores stale generations automatically.
        if worker in self._active_thumbnail_workers:
            _LOGGER.error("Light thumbnail generation failed: %s", message)

    @Slot(int)
    def _on_thumbnail_finished(self, worker: ThumbnailGeneratorWorker, generation_id: int) -> None:
        """Release references to completed workers so they can be garbage collected."""

        del generation_id
        try:
            self._active_thumbnail_workers.remove(worker)
        except ValueError:
            pass


class _SliderRow(QFrame):
    """Helper widget bundling a label, slider and numeric read-out."""

    uiValueChanged = Signal(str, float)
    """Emitted whenever the slider's visual value changes due to user interaction."""

    clickedWhenDisabled = Signal()

    def __init__(self, key: str, label: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._key = key
        self._session: Optional[EditSession] = None

        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.slider = BWSlider(label, self, minimum=-1.0, maximum=1.0, initial=0.0)
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