"""Edit page containing the adjustment controls and image preview."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..icon import load_icon
from .edit_sidebar import EditSidebar
from .edit_topbar import SegmentedTopBar
from .edit_viewer_host import EditViewerHost
from .main_window_metrics import (
    EDIT_DONE_BUTTON_BACKGROUND,
    EDIT_DONE_BUTTON_BACKGROUND_DISABLED,
    EDIT_DONE_BUTTON_BACKGROUND_HOVER,
    EDIT_DONE_BUTTON_BACKGROUND_PRESSED,
    EDIT_DONE_BUTTON_TEXT_COLOR,
    EDIT_DONE_BUTTON_TEXT_DISABLED,
    EDIT_HEADER_BUTTON_HEIGHT,
    HEADER_BUTTON_SIZE,
    HEADER_ICON_GLYPH_SIZE,
)


class EditPageWidget(QWidget):
    """Composite widget encapsulating the editing workflow UI."""

    def __init__(self, main_window: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("editPage")

        self.edit_mode_group = QActionGroup(main_window)
        self.edit_mode_group.setExclusive(True)

        self.edit_adjust_action = QAction(main_window)
        self.edit_adjust_action.setCheckable(True)
        self.edit_adjust_action.setChecked(True)
        self.edit_mode_group.addAction(self.edit_adjust_action)

        self.edit_crop_action = QAction(main_window)
        self.edit_crop_action.setCheckable(True)
        self.edit_mode_group.addAction(self.edit_crop_action)

        self.edit_compare_button = QToolButton(self)
        self.edit_reset_button = QPushButton(self)
        self.edit_done_button = QPushButton(self)
        self.edit_image_viewer = EditViewerHost()
        self.edit_sidebar = EditSidebar()
        self.edit_sidebar.setObjectName("editSidebar")

        self.edit_zoom_host = QWidget(self)
        self.edit_zoom_host_layout = QHBoxLayout(self.edit_zoom_host)
        self.edit_zoom_host_layout.setContentsMargins(0, 0, 0, 0)
        self.edit_zoom_host_layout.setSpacing(4)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.edit_header_container = self._build_header()
        layout.addWidget(self.edit_header_container)

        edit_body = QWidget(self)
        edit_body_layout = QHBoxLayout(edit_body)
        edit_body_layout.setContentsMargins(0, 0, 0, 0)
        edit_body_layout.setSpacing(12)
        edit_body_layout.addWidget(self.edit_image_viewer, 1)
        edit_body_layout.addWidget(self.edit_sidebar)
        layout.addWidget(edit_body, 1)

        default_sidebar_min = self.edit_sidebar.minimumWidth()
        default_sidebar_max = self.edit_sidebar.maximumWidth()
        default_sidebar_hint = max(self.edit_sidebar.sizeHint().width(), default_sidebar_min)
        self.edit_sidebar.setProperty("defaultMinimumWidth", default_sidebar_min)
        self.edit_sidebar.setProperty("defaultMaximumWidth", default_sidebar_max)
        self.edit_sidebar.setProperty("defaultPreferredWidth", default_sidebar_hint)

        self.edit_sidebar.setMinimumWidth(0)
        self.edit_sidebar.setMaximumWidth(0)
        self.edit_sidebar.hide()

        self.edit_header_container.hide()

    def _build_header(self) -> QWidget:
        """Construct the edit toolbar containing compare/reset/done controls."""

        container = QWidget(self)
        container.setObjectName("editHeaderContainer")
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(12, 0, 12, 0)
        container_layout.setSpacing(12)

        left_controls_container = QWidget(container)
        left_controls_layout = QHBoxLayout(left_controls_container)
        left_controls_layout.setContentsMargins(0, 0, 0, 0)
        left_controls_layout.setSpacing(8)
        left_controls_container.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Preferred,
        )

        self.edit_compare_button.setIcon(load_icon("square.fill.and.line.vertical.and.square.svg"))
        self.edit_compare_button.setIconSize(HEADER_ICON_GLYPH_SIZE)
        self.edit_compare_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.edit_compare_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_compare_button.setAutoRaise(True)
        self.edit_compare_button.setFixedSize(HEADER_BUTTON_SIZE)
        left_controls_layout.addWidget(self.edit_compare_button)

        self.edit_reset_button.setAutoDefault(False)
        self.edit_reset_button.setDefault(False)
        self.edit_reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_reset_button.setFixedHeight(EDIT_HEADER_BUTTON_HEIGHT)
        left_controls_layout.addWidget(self.edit_reset_button)

        self.edit_zoom_host_layout.setContentsMargins(0, 0, 0, 0)
        self.edit_zoom_host_layout.setSpacing(4)
        left_controls_layout.addWidget(self.edit_zoom_host)

        container_layout.addWidget(left_controls_container)

        self.edit_mode_control = SegmentedTopBar(
            (
                self.edit_adjust_action.text() or "Adjust",
                self.edit_crop_action.text() or "Crop",
            ),
            container,
        )
        container_layout.addWidget(self.edit_mode_control, 0, Qt.AlignmentFlag.AlignHCenter)

        right_controls_container = QWidget(container)
        right_controls_layout = QHBoxLayout(right_controls_container)
        right_controls_layout.setContentsMargins(0, 0, 0, 0)
        right_controls_layout.setSpacing(8)
        right_controls_container.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Preferred,
        )

        self.edit_done_button.setObjectName("editDoneButton")
        self.edit_done_button.setAutoDefault(False)
        self.edit_done_button.setDefault(False)
        self.edit_done_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_done_button.setFixedHeight(30)
        self.edit_done_button.setStyleSheet(
            "QPushButton#editDoneButton {"
            f"  background-color: {EDIT_DONE_BUTTON_BACKGROUND};"
            "  border: none;"
            "  border-radius: 8px;"
            f"  color: {EDIT_DONE_BUTTON_TEXT_COLOR};"
            "  font-weight: 600;"
            "  padding-left: 20px;"
            "  padding-right: 20px;"
            "}"
            "QPushButton#editDoneButton:hover {"
            f"  background-color: {EDIT_DONE_BUTTON_BACKGROUND_HOVER};"
            "}"
            "QPushButton#editDoneButton:pressed {"
            f"  background-color: {EDIT_DONE_BUTTON_BACKGROUND_PRESSED};"
            "}"
            "QPushButton#editDoneButton:disabled {"
            f"  background-color: {EDIT_DONE_BUTTON_BACKGROUND_DISABLED};"
            f"  color: {EDIT_DONE_BUTTON_TEXT_DISABLED};"
            "}"
        )
        right_controls_layout.addWidget(self.edit_done_button)

        container_layout.addWidget(right_controls_container)
        self.edit_right_controls_layout = right_controls_layout

        return container


__all__ = ["EditPageWidget"]
