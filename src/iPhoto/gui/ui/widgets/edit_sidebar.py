"""Composite widget hosting the editing tool sections."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..models.edit_session import EditSession
from .edit_light_section import EditLightSection
from .collapsible_section import CollapsibleSection


class EditSidebar(QWidget):
    """Sidebar that exposes the available editing tools."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[EditSession] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        # Adjust page ---------------------------------------------------
        adjust_container = QWidget(self)
        adjust_layout = QVBoxLayout(adjust_container)
        adjust_layout.setContentsMargins(0, 0, 0, 0)
        adjust_layout.setSpacing(0)

        scroll = QScrollArea(adjust_container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        scroll_content = QWidget(scroll)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(12, 12, 12, 12)
        scroll_layout.setSpacing(12)

        self._light_section = EditLightSection(scroll_content)
        light_container = CollapsibleSection("Light", "sun.max.svg", self._light_section, scroll_content)
        scroll_layout.addWidget(light_container)

        scroll_layout.addWidget(self._build_separator(scroll_content))

        color_placeholder = QLabel("Color adjustments are coming soon.", scroll_content)
        color_placeholder.setWordWrap(True)
        color_container = CollapsibleSection("Color", "color.circle.svg", color_placeholder, scroll_content)
        color_container.set_expanded(False)
        scroll_layout.addWidget(color_container)

        scroll_layout.addWidget(self._build_separator(scroll_content))

        bw_placeholder = QLabel("Black & White adjustments are coming soon.", scroll_content)
        bw_placeholder.setWordWrap(True)
        bw_container = CollapsibleSection(
            "Black & White",
            "circle.lefthalf.fill.svg",
            bw_placeholder,
            scroll_content,
        )
        bw_container.set_expanded(False)
        scroll_layout.addWidget(bw_container)
        scroll_layout.addStretch(1)
        scroll_content.setLayout(scroll_layout)
        scroll.setWidget(scroll_content)

        adjust_layout.addWidget(scroll)
        adjust_container.setLayout(adjust_layout)
        self._stack.addWidget(adjust_container)

        # Crop page -----------------------------------------------------
        crop_placeholder = QLabel(
            "Cropping tools will arrive in a future update.",
            self,
        )
        crop_placeholder.setWordWrap(True)
        crop_placeholder.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        crop_container = QWidget(self)
        crop_layout = QVBoxLayout(crop_container)
        crop_layout.setContentsMargins(24, 24, 24, 24)
        crop_layout.addWidget(crop_placeholder)
        crop_layout.addStretch(1)
        crop_container.setLayout(crop_layout)
        self._stack.addWidget(crop_container)

        self.set_mode("adjust")

    # ------------------------------------------------------------------
    def set_session(self, session: Optional[EditSession]) -> None:
        """Attach *session* to every tool section."""

        self._session = session
        self._light_section.bind_session(session)

    def session(self) -> Optional[EditSession]:
        return self._session

    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        """Switch the visible page to *mode* (``"adjust"`` or ``"crop"``)."""

        index = 0 if mode == "adjust" else 1
        self._stack.setCurrentIndex(index)

    def refresh(self) -> None:
        """Force the currently visible sections to sync with the session."""

        self._light_section.refresh_from_session()

    def _build_separator(self, parent: QWidget) -> QFrame:
        """Return a subtle divider separating adjacent section headers."""

        separator = QFrame(parent)
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        separator.setStyleSheet("QFrame { background-color: palette(mid); }")
        separator.setFixedHeight(1)
        return separator
