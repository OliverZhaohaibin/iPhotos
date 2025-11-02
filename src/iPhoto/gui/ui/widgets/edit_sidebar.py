"""Composite widget hosting the editing tool sections."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ....core.light_resolver import LIGHT_KEYS
from ..models.edit_session import EditSession
from .edit_light_section import EditLightSection
from .collapsible_section import CollapsibleSection
from ..palette import SIDEBAR_BACKGROUND_COLOR
from ..icon import load_icon


class EditSidebar(QWidget):
    """Sidebar that exposes the available editing tools."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[EditSession] = None
        self._light_preview_image = None
        self._control_icon_tint: QColor | None = None
        # Track whether the Light header controls have active signal bindings so we can
        # disconnect them safely without triggering PySide warnings when no connection exists.
        self._light_controls_connected = False

        # Match the classic sidebar chrome so the edit tools retain the soft blue
        # background the rest of the application uses for navigation panes.
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, SIDEBAR_BACKGROUND_COLOR)
        palette.setColor(QPalette.ColorRole.Base, SIDEBAR_BACKGROUND_COLOR)
        self.setPalette(palette)
        self.setAutoFillBackground(True)

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

        # Ensure the scroll surface shares the same tint so the viewport and the
        # surrounding frame render as a single continuous panel.
        scroll_palette = scroll.palette()
        scroll_palette.setColor(QPalette.ColorRole.Base, SIDEBAR_BACKGROUND_COLOR)
        scroll_palette.setColor(QPalette.ColorRole.Window, SIDEBAR_BACKGROUND_COLOR)
        scroll.setPalette(scroll_palette)

        scroll_content = QWidget(scroll)
        # Allow the scroll area content to compress to zero width during the edit transition.  The
        # animated splitter reduces the sidebar to a sliver before hiding it entirely, so the
        # interior widget hierarchy must advertise that no minimum space is required; otherwise Qt
        # clamps the collapse and the sidebar appears to "pop" out of existence.
        scroll_content.setMinimumWidth(0)
        scroll_content_palette = scroll_content.palette()
        scroll_content_palette.setColor(QPalette.ColorRole.Window, SIDEBAR_BACKGROUND_COLOR)
        scroll_content_palette.setColor(QPalette.ColorRole.Base, SIDEBAR_BACKGROUND_COLOR)
        scroll_content.setPalette(scroll_content_palette)
        scroll_content.setAutoFillBackground(True)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(12, 12, 12, 12)
        scroll_layout.setSpacing(12)

        self._light_section = EditLightSection(scroll_content)
        self._light_section_container = CollapsibleSection(
            "Light",
            "sun.max.svg",
            self._light_section,
            scroll_content,
        )

        self.light_reset_button = QToolButton(self._light_section_container)
        self.light_reset_button.setAutoRaise(True)
        self.light_reset_button.setIcon(load_icon("arrow.uturn.left.svg"))
        self.light_reset_button.setToolTip("Reset Light adjustments")

        self.light_toggle_button = QToolButton(self._light_section_container)
        self.light_toggle_button.setAutoRaise(True)
        self.light_toggle_button.setCheckable(True)
        self.light_toggle_button.setIcon(load_icon("circle.svg"))
        self.light_toggle_button.setToolTip("Toggle Light adjustments")

        self._light_section_container.add_header_control(self.light_reset_button)
        self._light_section_container.add_header_control(self.light_toggle_button)

        scroll_layout.addWidget(self._light_section_container)

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
        crop_palette = crop_container.palette()
        crop_palette.setColor(QPalette.ColorRole.Window, SIDEBAR_BACKGROUND_COLOR)
        crop_palette.setColor(QPalette.ColorRole.Base, SIDEBAR_BACKGROUND_COLOR)
        crop_container.setPalette(crop_palette)
        crop_container.setAutoFillBackground(True)
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

        if self._light_controls_connected:
            # Only attempt to disconnect signal handlers when they are known to be bound.
            # PySide prints RuntimeWarnings if disconnect() is invoked without a matching
            # connection, so guarding the call keeps the debug console clean when the editor
            # is repeatedly opened and closed.
            try:
                self.light_reset_button.clicked.disconnect(self._on_light_reset)
            except (TypeError, RuntimeError):
                # If Qt reports that the slot is already disconnected we simply clear the flag.
                pass
            try:
                self.light_toggle_button.toggled.disconnect(self._on_light_toggled)
            except (TypeError, RuntimeError):
                pass

            if self._session is not None:
                try:
                    self._session.valueChanged.disconnect(self._on_session_value_changed)
                except (TypeError, RuntimeError):
                    pass

            self._light_controls_connected = False

        self._session = session
        self._light_section.bind_session(session)
        if session is not None:
            self.light_reset_button.clicked.connect(self._on_light_reset)
            self.light_toggle_button.toggled.connect(self._on_light_toggled)
            session.valueChanged.connect(self._on_session_value_changed)
            self._light_controls_connected = True
            self._sync_light_toggle_state()
            if self._light_preview_image is not None:
                self._light_section.set_preview_image(self._light_preview_image)
        else:
            self.light_toggle_button.setChecked(False)
            self._update_light_toggle_icon(False)

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
        self._sync_light_toggle_state()

    def set_light_preview_image(self, image) -> None:
        """Provide *image* to the Light section for generating slider thumbnails."""

        self._light_preview_image = image
        self._light_section.set_preview_image(image)

    def _build_separator(self, parent: QWidget) -> QFrame:
        """Return a subtle divider separating adjacent section headers."""

        separator = QFrame(parent)
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        separator.setStyleSheet("QFrame { background-color: palette(mid); }")
        separator.setFixedHeight(1)
        return separator

    def _on_light_reset(self) -> None:
        if self._session is None:
            return
        updates = {key: 0.0 for key in LIGHT_KEYS}
        updates["Light_Master"] = 0.0
        updates["Light_Enabled"] = True
        self._session.set_values(updates)
        self._light_section.refresh_from_session()
        self._sync_light_toggle_state()

    def _on_light_toggled(self, checked: bool) -> None:
        self._update_light_toggle_icon(checked)
        if self._session is None:
            return
        self._session.set_value("Light_Enabled", checked)

    @Slot(str, object)  # 使用 object 以匹配 (float | bool)
    def _on_session_value_changed(self, key: str, value: object) -> None:
        """Listen for session changes (e.g., from sliders) to sync the toggle button."""
        del value  # 我们只关心键，不关心具体的值
        if key == "Light_Enabled":
            self._sync_light_toggle_state()

    def _sync_light_toggle_state(self) -> None:
        if self._session is None:
            block = self.light_toggle_button.blockSignals(True)
            self.light_toggle_button.setChecked(False)
            self.light_toggle_button.blockSignals(block)
            self._update_light_toggle_icon(False)
            return
        enabled = bool(self._session.value("Light_Enabled"))
        block = self.light_toggle_button.blockSignals(True)
        self.light_toggle_button.setChecked(enabled)
        self.light_toggle_button.blockSignals(block)
        self._update_light_toggle_icon(enabled)

    def _update_light_toggle_icon(self, enabled: bool) -> None:
        if enabled:
            self.light_toggle_button.setIcon(load_icon("checkmark.circle.svg"))
        else:
            tint_name = self._control_icon_tint.name(QColor.NameFormat.HexArgb) if self._control_icon_tint else None
            self.light_toggle_button.setIcon(load_icon("circle.svg", color=tint_name))


    def set_control_icon_tint(self, color: QColor | None) -> None:
        """Apply a color tint to all header control icons."""
        self._control_icon_tint = color
        tint_name = color.name(QColor.NameFormat.HexArgb) if color else None
        self.light_reset_button.setIcon(
            load_icon("arrow.uturn.left.svg", color=tint_name)
        )
        self._sync_light_toggle_state()