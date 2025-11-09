"""Placeholder widget for the upcoming crop tool sidebar."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class EditCropSection(QWidget):
    """Temporary sidebar surface shown when crop mode is active.

    The crop workflow is being migrated from a prototype implementation.  Until
    the dedicated controls (aspect ratio toggles, rotation sliders, etc.) are
    implemented the sidebar needs a stable placeholder so the edit transition
    animation retains a consistent layout.  The widget simply centres an
    informational label and can be replaced with the full UI in a future
    update.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addStretch(1)

        label = QLabel(
            "Crop adjustments will surface here in a future update.",
            self,
        )
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        layout.addStretch(1)

        self.setLayout(layout)

