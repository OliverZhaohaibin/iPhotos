"""Black & White adjustment section for the edit sidebar."""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..models.edit_session import EditSession
from .edit_strip import BWSlider


class EditBWSection(QWidget):
    """Expose the GPU-only black & white adjustments as a set of sliders."""

    adjustmentChanged = Signal(str, float)
    """Emitted when one of the sliders modifies its backing adjustment."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[EditSession] = None
        self._sliders: Dict[str, BWSlider] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # The four adjustments mirror the standalone BW demo.  Each slider operates on
        # normalised ``[-1.0, 1.0]`` or ``[0.0, 1.0]`` ranges so the UI remains predictable
        # even when the values are serialised for sidecars.
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
            self._session.valueChanged.disconnect(self._on_session_value_changed)
            self._session.resetPerformed.disconnect(self._on_session_reset)

        self._session = session

        if session is not None:
            session.valueChanged.connect(self._on_session_value_changed)
            session.resetPerformed.connect(self._on_session_reset)
            self.refresh_from_session()
            self.setEnabled(True)
        else:
            self._reset_slider_values()
            self.setEnabled(False)

    def refresh_from_session(self) -> None:
        """Synchronise the slider positions with the active session state."""

        if self._session is None:
            self._reset_slider_values()
            self.setEnabled(False)
            return

        for key, slider in self._sliders.items():
            slider.blockSignals(True)
            slider.setValue(float(self._session.value(key)), emit=False)
            slider.blockSignals(False)

    # ------------------------------------------------------------------
    def _reset_slider_values(self) -> None:
        """Restore every slider to its neutral position."""

        for slider in self._sliders.values():
            slider.blockSignals(True)
            slider.setValue(0.0, emit=False)
            slider.blockSignals(False)

    def _handle_slider_changed(self, key: str, new_value: float) -> None:
        """Persist the slider change back into the editing session."""

        if self._session is None:
            return
        self._session.set_value(key, new_value)
        self.adjustmentChanged.emit(key, new_value)

    @Slot(str, object)
    def _on_session_value_changed(self, key: str, value: object) -> None:
        """Update the matching slider when *key* changes externally."""

        if key not in self._sliders:
            return
        slider = self._sliders[key]
        slider.blockSignals(True)
        slider.setValue(float(value), emit=False)
        slider.blockSignals(False)

    @Slot()
    def _on_session_reset(self) -> None:
        """Refresh the UI after the session has been reset to defaults."""

        self.refresh_from_session()

