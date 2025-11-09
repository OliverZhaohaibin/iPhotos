"""Header row containing the menu bar and primary toolbar buttons."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QPalette
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenuBar,
    QSizePolicy,
    QSpacerItem,
    QToolButton,
    QWidget,
)


class MainHeaderWidget(QWidget):
    """Container hosting the menu bar alongside quick access buttons."""

    def __init__(self, parent: QWidget | None, main_window: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("menuBarContainer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.menu_bar = QMenuBar(self)
        self.menu_bar.setObjectName("chromeMenuBar")
        self.menu_bar.setNativeMenuBar(False)
        self.menu_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.menu_bar.setAutoFillBackground(True)
        self.menu_bar.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )

        layout.addWidget(self.menu_bar)
        layout.addSpacerItem(
            QSpacerItem(
                1,
                1,
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Minimum,
            )
        )

        self.rescan_button = QToolButton(self)
        self.rescan_button.setObjectName("rescanButton")
        self.rescan_button.setAutoRaise(True)
        layout.addWidget(self.rescan_button)

        self.selection_button = QToolButton(self)
        self.selection_button.setObjectName("selectionButton")
        self.selection_button.setAutoRaise(True)
        self.selection_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        layout.addWidget(self.selection_button)

        self._synchronise_palettes()
        self._create_actions(main_window)
        self._populate_menus()

    def _synchronise_palettes(self) -> None:
        """Ensure the container and menu bar share the same opaque background."""

        menu_palette = self.menu_bar.palette()
        menu_palette.setColor(
            QPalette.ColorRole.Window,
            menu_palette.color(QPalette.ColorRole.Base),
        )
        self.menu_bar.setPalette(menu_palette)

        container_palette = self.palette()
        container_palette.setColor(
            QPalette.ColorRole.Window,
            menu_palette.color(QPalette.ColorRole.Base),
        )
        self.setPalette(container_palette)

    def _create_actions(self, main_window: QWidget) -> None:
        """Instantiate the :class:`QAction` objects exposed to controllers."""

        self.open_album_action = QAction("Open Album Folder…", main_window)
        self.rescan_action = QAction("Rescan", main_window)
        self.rebuild_links_action = QAction("Rebuild Live Links", main_window)
        self.bind_library_action = QAction("Set Basic Library…", main_window)
        self.toggle_filmstrip_action = QAction(
            "Show Filmstrip", main_window, checkable=True
        )
        self.toggle_filmstrip_action.setChecked(True)

        self.share_action_group = QActionGroup(main_window)
        self.share_action_copy_file = QAction("Copy File", main_window, checkable=True)
        self.share_action_copy_path = QAction("Copy Path", main_window, checkable=True)
        self.share_action_reveal_file = QAction(
            "Reveal in File Manager", main_window, checkable=True
        )
        self.share_action_group.addAction(self.share_action_copy_file)
        self.share_action_group.addAction(self.share_action_copy_path)
        self.share_action_group.addAction(self.share_action_reveal_file)
        self.share_action_reveal_file.setChecked(True)

        self.wheel_action_group = QActionGroup(main_window)
        self.wheel_action_navigate = QAction("Navigate", main_window, checkable=True)
        self.wheel_action_zoom = QAction("Zoom", main_window, checkable=True)
        self.wheel_action_group.addAction(self.wheel_action_navigate)
        self.wheel_action_group.addAction(self.wheel_action_zoom)
        self.wheel_action_navigate.setChecked(True)

    def _populate_menus(self) -> None:
        """Populate the menu bar and wire shared actions to widgets."""

        file_menu = self.menu_bar.addMenu("&File")
        for action in (
            self.open_album_action,
            None,
            self.bind_library_action,
            None,
            self.rebuild_links_action,
        ):
            if action is None:
                file_menu.addSeparator()
            else:
                file_menu.addAction(action)

        self.rescan_button.setDefaultAction(self.rescan_action)

        settings_menu = self.menu_bar.addMenu("&Settings")
        settings_menu.addAction(self.bind_library_action)
        settings_menu.addSeparator()
        settings_menu.addAction(self.toggle_filmstrip_action)
        settings_menu.addSeparator()

        wheel_menu = settings_menu.addMenu("Wheel Action")
        wheel_menu.addAction(self.wheel_action_navigate)
        wheel_menu.addAction(self.wheel_action_zoom)

        share_menu = settings_menu.addMenu("Share Action")
        share_menu.addAction(self.share_action_copy_file)
        share_menu.addAction(self.share_action_copy_path)
        share_menu.addAction(self.share_action_reveal_file)


__all__ = ["MainHeaderWidget"]
