"""Light adjustment section used inside the edit sidebar."""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...models.edit_session import EditSession


class EditLightSection(QWidget):
    """Container widget hosting the "Light" adjustment sliders."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[EditSession] = None
        self._rows: Dict[str, _SliderRow] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        options_group = QGroupBox("Options", self)
        options_layout = QGridLayout(options_group)
        options_layout.setContentsMargins(12, 12, 12, 12)
        options_layout.setHorizontalSpacing(12)
        options_layout.setVerticalSpacing(10)

        labels = [
            ("Brilliance", "Brilliance"),
            ("Exposure", "Exposure"),
            ("Highlights", "Highlights"),
            ("Shadows", "Shadows"),
            ("Brightness", "Brightness"),
            ("Contrast", "Contrast"),
            ("Black Point", "BlackPoint"),
        ]
        for row_index, (label_text, key) in enumerate(labels):
            row = _SliderRow(key, label_text, parent=options_group)
            options_layout.addWidget(row.name_label, row_index, 0)
            options_layout.addWidget(row.slider, row_index, 1)
            options_layout.addWidget(row.value_label, row_index, 2)
            self._rows[key] = row

        layout.addWidget(options_group)
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
            row.set_session(session)
        if session is not None:
            session.valueChanged.connect(self._on_session_value_changed)
            session.resetPerformed.connect(self._on_session_reset)
            self.refresh_from_session()
        else:
            self._disable_rows()

    def refresh_from_session(self) -> None:
        """Synchronise slider positions with the attached session."""

        if self._session is None:
            self._disable_rows()
            return
        for key, row in self._rows.items():
            row.update_from_value(self._session.value(key))
            row.setEnabled(True)

    def _disable_rows(self) -> None:
        for row in self._rows.values():
            row.setEnabled(False)
            row.update_from_value(0.0)

    # ------------------------------------------------------------------
    def _on_session_value_changed(self, key: str, value: float) -> None:
        row = self._rows.get(key)
        if row is None:
            return
        row.update_from_value(value)

    def _on_session_reset(self) -> None:
        self.refresh_from_session()


class _SliderRow(QFrame):
    """Helper widget bundling a label, slider and numeric read-out."""

    def __init__(self, key: str, label: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._key = key
        self._session: Optional[EditSession] = None

        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)

        self.name_label = QLabel(label, self)
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(-100, 100)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.valueChanged.connect(self._handle_slider_changed)

        self.value_label = QLabel("0.00", self)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value_label.setMinimumWidth(48)

        layout.addWidget(self.name_label, 0, 0)
        layout.addWidget(self.slider, 0, 1)
        layout.addWidget(self.value_label, 0, 2)

    # ------------------------------------------------------------------
    def set_session(self, session: Optional[EditSession]) -> None:
        self._session = session

    def setEnabled(self, enabled: bool) -> None:  # type: ignore[override]
        super().setEnabled(enabled)
        self.slider.setEnabled(enabled)

    def update_from_value(self, value: float) -> None:
        self.value_label.setText(f"{value:+.2f}")
        block = self.slider.blockSignals(True)
        try:
            self.slider.setValue(int(round(value * 100)))
        finally:
            self.slider.blockSignals(block)

    # ------------------------------------------------------------------
    def _handle_slider_changed(self, raw_value: int) -> None:
        if self._session is None:
            return
        self.value_label.setText(f"{raw_value / 100:+.2f}")
        self._session.set_value(self._key, raw_value / 100.0)
