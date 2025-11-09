"""Manage the edit mode theme separate from transition animations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from ..icon import load_icon
from ..palette import SIDEBAR_BACKGROUND_COLOR
from ..ui_main_window import Ui_MainWindow
from ..widgets.collapsible_section import CollapsibleSection
from ..window_manager import RoundedWindowShell

if TYPE_CHECKING:
    from .detail_ui_controller import DetailUIController


class EditThemeManager:
    """Handle theme caching and application for the edit interface."""

    EDIT_DARK_STYLESHEET = "\n".join(
        [
            "QWidget#editPage {",
            "  background-color: #1C1C1E;",
            "}",
            "QWidget#editPage QLabel,",
            "QWidget#editPage QToolButton,",
            "QWidget#editHeaderContainer QPushButton {",
            "  color: #F5F5F7;",
            "}",
            "QWidget#editHeaderContainer {",
            "  background-color: #2C2C2E;",
            "  border-radius: 12px;",
            "}",
            "QWidget#editPage EditSidebar,",
            "QWidget#editPage EditSidebar QWidget,",
            "QWidget#editPage QScrollArea,",
            "QWidget#editPage QScrollArea > QWidget {",
            "  background-color: #2C2C2E;",
            "  color: #F5F5F7;",
            "}",
            "QWidget#editPage QGroupBox {",
            "  background-color: #1F1F1F;",
            "  border: 1px solid #323236;",
            "  border-radius: 10px;",
            "  margin-top: 24px;",
            "  padding-top: 12px;",
            "}",
            "QWidget#editPage QGroupBox::title {",
            "  color: #F5F5F7;",
            "  subcontrol-origin: margin;",
            "  left: 12px;",
            "  padding: 0 4px;",
            "}",
            "QWidget#editPage #collapsibleSection QLabel {",
            "  color: #F5F5F7;",
            "}",
        ]
    )

    def __init__(self, ui: Ui_MainWindow, window: QObject | None) -> None:
        """Cache all light-theme defaults that must be restored later."""

        self._ui = ui
        self._window = window
        self._detail_ui_controller: "DetailUIController" | None = None

        self._edit_container = ui.detail_page.edit_container
        self._default_edit_page_stylesheet = self._edit_container.styleSheet()
        self._default_sidebar_stylesheet = ui.sidebar.styleSheet()
        self._default_statusbar_stylesheet = ui.status_bar.styleSheet()
        self._default_window_chrome_stylesheet = ui.window_chrome.styleSheet()
        self._default_window_shell_stylesheet = ui.window_shell.styleSheet()
        self._default_title_bar_stylesheet = ui.title_bar.styleSheet()
        self._default_title_separator_stylesheet = ui.title_separator.styleSheet()
        self._default_menu_bar_container_stylesheet = ui.menu_bar_container.styleSheet()
        self._default_menu_bar_stylesheet = ui.menu_bar.styleSheet()
        self._default_rescan_button_stylesheet = ui.rescan_button.styleSheet()

        shell_parent = ui.window_shell.parentWidget()
        self._rounded_window_shell: RoundedWindowShell | None = (
            shell_parent if isinstance(shell_parent, RoundedWindowShell) else None
        )

        self._default_sidebar_palette = QPalette(ui.sidebar.palette())
        self._default_statusbar_palette = QPalette(ui.status_bar.palette())
        self._default_window_chrome_palette = QPalette(ui.window_chrome.palette())
        self._default_window_shell_palette = QPalette(ui.window_shell.palette())
        self._default_title_bar_palette = QPalette(ui.title_bar.palette())
        self._default_title_separator_palette = QPalette(ui.title_separator.palette())
        self._default_menu_bar_container_palette = QPalette(ui.menu_bar_container.palette())
        self._default_menu_bar_palette = QPalette(ui.menu_bar.palette())
        self._default_rescan_button_palette = QPalette(ui.rescan_button.palette())
        self._default_selection_button_palette = QPalette(ui.selection_button.palette())
        self._default_selection_button_stylesheet = ui.selection_button.styleSheet()
        self._default_selection_button_autofill = ui.selection_button.autoFillBackground()
        self._default_window_title_palette = QPalette(ui.window_title_label.palette())
        self._default_window_title_stylesheet = ui.window_title_label.styleSheet()
        self._default_sidebar_tree_palette = QPalette(ui.sidebar._tree.palette())
        self._default_statusbar_message_palette = QPalette(
            ui.status_bar._message_label.palette()
        )

        self._default_sidebar_autofill = ui.sidebar.autoFillBackground()
        self._default_statusbar_autofill = ui.status_bar.autoFillBackground()
        self._default_window_chrome_autofill = ui.window_chrome.autoFillBackground()
        self._default_window_shell_autofill = ui.window_shell.autoFillBackground()
        self._default_title_bar_autofill = ui.title_bar.autoFillBackground()
        self._default_title_separator_autofill = ui.title_separator.autoFillBackground()
        self._default_menu_bar_container_autofill = (
            ui.menu_bar_container.autoFillBackground()
        )
        self._default_menu_bar_autofill = ui.menu_bar.autoFillBackground()
        self._default_rescan_button_autofill = ui.rescan_button.autoFillBackground()
        self._default_sidebar_tree_autofill = ui.sidebar._tree.autoFillBackground()

        if self._rounded_window_shell is not None:
            self._default_rounded_shell_palette = QPalette(
                self._rounded_window_shell.palette()
            )
            self._default_rounded_shell_override: QColor | None = getattr(
                self._rounded_window_shell, "_override_color", None
            )
        else:
            self._default_rounded_shell_palette = None
            self._default_rounded_shell_override = None

        self._edit_theme_applied = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_detail_ui_controller(
        self, controller: "DetailUIController" | None
    ) -> None:
        """Store *controller* so toolbar icon tinting follows theme changes."""

        self._detail_ui_controller = controller
        if controller is None:
            return
        if self._edit_theme_applied:
            controller.set_toolbar_icon_tint(QColor("#FFFFFF"))
        else:
            controller.set_toolbar_icon_tint(None)

    def apply_dark_theme(self) -> None:
        """Activate the dark edit palette across the entire window chrome."""

        if self._edit_theme_applied:
            return
        self._edit_container.setStyleSheet(self.EDIT_DARK_STYLESHEET)
        self._ui.edit_image_viewer.set_surface_color_override("#111111")

        dark_icon_color = QColor("#FFFFFF")
        dark_icon_hex = dark_icon_color.name(QColor.NameFormat.HexArgb)
        ICONS_WITH_NATIVE_COLOR = {
            "color.circle.svg",
            "checkmark.svg"
        }
        self._ui.edit_compare_button.setIcon(
            load_icon(
                "square.fill.and.line.vertical.and.square.svg",
                color=dark_icon_hex,
            )
        )
        for section in self._ui.edit_sidebar.findChildren(CollapsibleSection):
            section.set_toggle_icon_tint(dark_icon_color)
            icon_label = getattr(section, "_icon_label", None)
            icon_name = getattr(section, "_icon_name", "")
            if icon_label is not None and icon_name:
                if icon_name in ICONS_WITH_NATIVE_COLOR:
                    # If the icon has its own colour, the “colour” parameter is not passed.
                    icon_label.setPixmap(
                        load_icon(icon_name).pixmap(20, 20)
                    )
                else:
                    # Otherwise for section in self._ui.edit_sidebar.findChildren(CollapsibleSection):colour it in the dark theme's colour.
                    icon_label.setPixmap(
                        load_icon(icon_name, color=dark_icon_hex).pixmap(20, 20)
                    )

        self._ui.edit_sidebar.set_control_icon_tint(dark_icon_color)
        self._ui.zoom_out_button.setIcon(load_icon("minus.svg", color=dark_icon_hex))
        self._ui.zoom_in_button.setIcon(load_icon("plus.svg", color=dark_icon_hex))

        if self._detail_ui_controller is not None:
            self._detail_ui_controller.set_toolbar_icon_tint(dark_icon_color)
        else:
            self._ui.info_button.setIcon(
                load_icon("info.circle.svg", color=dark_icon_hex)
            )
            self._ui.favorite_button.setIcon(
                load_icon("suit.heart.svg", color=dark_icon_hex)
            )

        dark_palette = QPalette()
        window_color = QColor("#1C1C1E")
        button_color = QColor("#2C2C2E")
        text_color = QColor("#F5F5F7")
        disabled_text = QColor("#7F7F7F")
        accent_color = QColor("#0A84FF")
        outline_color = QColor("#323236")
        placeholder_text = QColor(245, 245, 247, 160)

        dark_palette.setColor(QPalette.ColorRole.Window, window_color)
        dark_palette.setColor(QPalette.ColorRole.Base, window_color)
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#242426"))
        dark_palette.setColor(QPalette.ColorRole.WindowText, text_color)
        dark_palette.setColor(QPalette.ColorRole.Text, text_color)
        dark_palette.setColor(QPalette.ColorRole.Button, button_color)
        dark_palette.setColor(QPalette.ColorRole.ButtonText, text_color)
        dark_palette.setColor(QPalette.ColorRole.BrightText, QColor("#FFFFFF"))
        dark_palette.setColor(QPalette.ColorRole.Link, accent_color)
        dark_palette.setColor(QPalette.ColorRole.Highlight, QColor("#3A3A3C"))
        dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        dark_palette.setColor(QPalette.ColorRole.PlaceholderText, placeholder_text)
        dark_palette.setColor(QPalette.ColorRole.Mid, outline_color)
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, button_color)
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, text_color)
        dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.ButtonText,
            disabled_text,
        )
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.WindowText,
            disabled_text,
        )
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.Highlight,
            QColor("#2C2C2E"),
        )
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.HighlightedText,
            disabled_text,
        )
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.PlaceholderText,
            QColor(160, 160, 160, 160),
        )

        widgets_to_update = [
            self._ui.sidebar,
            self._ui.status_bar,
            self._ui.window_chrome,
            self._ui.menu_bar_container,
            self._ui.menu_bar,
            self._ui.title_bar,
            self._ui.title_separator,
        ]
        for widget in widgets_to_update:
            widget.setPalette(dark_palette)
            widget.setAutoFillBackground(False)

        self._ui.rescan_button.setPalette(dark_palette)
        self._ui.rescan_button.setAutoFillBackground(False)

        self._ui.window_shell.setPalette(dark_palette)
        self._ui.window_shell.setAutoFillBackground(False)

        if self._rounded_window_shell is not None:
            self._rounded_window_shell.setPalette(dark_palette)
            self._rounded_window_shell.set_override_color(window_color)

        self._ui.sidebar._tree.setPalette(dark_palette)
        self._ui.sidebar._tree.setAutoFillBackground(False)
        self._ui.status_bar._message_label.setPalette(dark_palette)
        self._ui.selection_button.setPalette(dark_palette)
        self._ui.selection_button.setAutoFillBackground(False)
        self._ui.window_title_label.setPalette(dark_palette)

        self._refresh_menu_styles()
        self._ui.menu_bar.setAutoFillBackground(False)

        foreground_color = text_color.name()
        accent_color_name = accent_color.name()
        outline_color_name = outline_color.name()
        disabled_text_name = disabled_text.name()

        # The custom title bar label renders plain text without an icon, so explicitly override
        # its foreground colour to keep the "iPhoto" caption legible in both light and dark
        # chrome.  Using a direct ``color`` property avoids Qt inheriting an outdated palette
        # from a parent widget when the application toggles modes repeatedly.
        self._ui.window_title_label.setStyleSheet(f"color: {foreground_color};")

        self._ui.sidebar.setStyleSheet(
            "\n".join(
                [
                    "QWidget#albumSidebar {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QWidget#albumSidebar QLabel {",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        self._ui.status_bar.setStyleSheet(
            "\n".join(
                [
                    "QWidget#chromeStatusBar {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QWidget#chromeStatusBar QLabel {",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        self._ui.title_bar.setStyleSheet(
            "\n".join(
                [
                    "QWidget#windowTitleBar {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QWidget#windowTitleBar QLabel {",
                    f"  color: {foreground_color};",
                    "}",
                    "QWidget#windowTitleBar QToolButton {",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        self._ui.title_separator.setStyleSheet(
            "QFrame#windowTitleSeparator {"
            f"  background-color: {outline_color_name};"
            "  border: none;"
            "}"
        )
        self._ui.menu_bar.setStyleSheet(
            "\n".join(
                [
                    "QMenuBar#chromeMenuBar {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QMenuBar#chromeMenuBar::item {",
                    f"  color: {foreground_color};",
                    "}",
                    "QMenuBar#chromeMenuBar::item:selected {",
                    f"  background-color: {outline_color_name};",
                    "  border-radius: 6px;",
                    "}",
                    "QMenuBar#chromeMenuBar::item:pressed {",
                    f"  background-color: {accent_color_name};",
                    "}",
                ]
            )
        )
        self._ui.menu_bar_container.setStyleSheet(
            "\n".join(
                [
                    "QWidget#menuBarContainer {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        self._ui.rescan_button.setStyleSheet(
            "\n".join(
                [
                    "QToolButton#rescanButton {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QToolButton#rescanButton:disabled {",
                    "  background-color: transparent;",
                    f"  color: {disabled_text_name};",
                    "}",
                ]
            )
        )
        self._ui.selection_button.setStyleSheet(
            "\n".join(
                [
                    "QToolButton#selectionButton {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QToolButton#selectionButton:disabled {",
                    "  background-color: transparent;",
                    f"  color: {disabled_text_name};",
                    "}",
                ]
            )
        )
        self._ui.window_chrome.setStyleSheet(
            "\n".join(
                [
                    "background-color: transparent;",
                    f"color: {foreground_color};",
                ]
            )
        )

        self._edit_container.setStyleSheet(self.EDIT_DARK_STYLESHEET)
        self._edit_theme_applied = True

    def restore_light_theme(self) -> None:
        """Restore the light application chrome after leaving edit mode."""

        if not self._edit_theme_applied:
            return

        self._edit_container.setStyleSheet(self._default_edit_page_stylesheet)
        self._ui.edit_image_viewer.set_surface_color_override(None)
        self._ui.edit_sidebar.set_control_icon_tint(None)
        self._ui.edit_compare_button.setIcon(
            load_icon("square.fill.and.line.vertical.and.square.svg")
        )
        for section in self._ui.edit_sidebar.findChildren(CollapsibleSection):
            section.set_toggle_icon_tint(None)
            icon_label = getattr(section, "_icon_label", None)
            icon_name = getattr(section, "_icon_name", "")
            if icon_label is not None and icon_name:
                icon_label.setPixmap(load_icon(icon_name).pixmap(20, 20))

        self._ui.zoom_out_button.setIcon(load_icon("minus.svg"))
        self._ui.zoom_in_button.setIcon(load_icon("plus.svg"))
        if self._detail_ui_controller is not None:
            self._detail_ui_controller.set_toolbar_icon_tint(None)
        else:
            self._ui.info_button.setIcon(load_icon("info.circle.svg"))
            self._ui.favorite_button.setIcon(load_icon("suit.heart.svg"))

        widgets_to_restore = [
            (
                self._ui.sidebar,
                self._default_sidebar_palette,
                self._default_sidebar_autofill,
            ),
            (
                self._ui.status_bar,
                self._default_statusbar_palette,
                self._default_statusbar_autofill,
            ),
            (
                self._ui.window_chrome,
                self._default_window_chrome_palette,
                self._default_window_chrome_autofill,
            ),
            (
                self._ui.window_shell,
                self._default_window_shell_palette,
                self._default_window_shell_autofill,
            ),
            (
                self._ui.title_bar,
                self._default_title_bar_palette,
                self._default_title_bar_autofill,
            ),
            (
                self._ui.title_separator,
                self._default_title_separator_palette,
                self._default_title_separator_autofill,
            ),
            (
                self._ui.menu_bar_container,
                self._default_menu_bar_container_palette,
                self._default_menu_bar_container_autofill,
            ),
            (
                self._ui.menu_bar,
                self._default_menu_bar_palette,
                self._default_menu_bar_autofill,
            ),
            (
                self._ui.rescan_button,
                self._default_rescan_button_palette,
                self._default_rescan_button_autofill,
            ),
        ]
        for widget, palette, autofill in widgets_to_restore:
            if palette is not None:
                widget.setPalette(QPalette(palette))
            widget.setAutoFillBackground(bool(autofill))

        self._ui.selection_button.setPalette(
            QPalette(self._default_selection_button_palette)
        )
        self._ui.selection_button.setAutoFillBackground(
            self._default_selection_button_autofill
        )
        self._ui.window_title_label.setPalette(
            QPalette(self._default_window_title_palette)
        )
        self._apply_color_reset_stylesheet(
            self._ui.selection_button,
            self._default_selection_button_stylesheet,
            "QToolButton#selectionButton",
        )
        self._apply_color_reset_stylesheet(
            self._ui.window_title_label,
            self._default_window_title_stylesheet,
            "QLabel#windowTitleLabel",
        )
        self._ui.sidebar._tree.setPalette(
            QPalette(self._default_sidebar_tree_palette)
        )
        self._ui.sidebar._tree.setAutoFillBackground(
            self._default_sidebar_tree_autofill
        )
        self._ui.status_bar._message_label.setPalette(
            QPalette(self._default_statusbar_message_palette)
        )

        sidebar_stylesheet = self._default_sidebar_stylesheet
        if sidebar_stylesheet:
            self._ui.sidebar.setStyleSheet(sidebar_stylesheet)
        else:
            fallback_sidebar_stylesheet = "\n".join(
                [
                    "QWidget#albumSidebar {",
                    f"    background-color: {SIDEBAR_BACKGROUND_COLOR.name()};",
                    "}",
                ]
            )
            self._ui.sidebar.setStyleSheet(fallback_sidebar_stylesheet)
        self._ui.status_bar.setStyleSheet(self._default_statusbar_stylesheet)
        self._ui.window_chrome.setStyleSheet(self._default_window_chrome_stylesheet)
        self._ui.window_shell.setStyleSheet(self._default_window_shell_stylesheet)
        self._ui.title_bar.setStyleSheet(self._default_title_bar_stylesheet)
        self._ui.title_separator.setStyleSheet(
            self._default_title_separator_stylesheet
        )
        self._ui.menu_bar_container.setStyleSheet(
            self._default_menu_bar_container_stylesheet
        )
        self._ui.menu_bar.setStyleSheet(self._default_menu_bar_stylesheet)
        self._apply_color_reset_stylesheet(
            self._ui.rescan_button,
            self._default_rescan_button_stylesheet,
            "QToolButton#rescanButton",
        )

        if self._rounded_window_shell is not None:
            if self._default_rounded_shell_palette is not None:
                self._rounded_window_shell.setPalette(
                    QPalette(self._default_rounded_shell_palette)
                )
            self._rounded_window_shell.set_override_color(
                self._default_rounded_shell_override
            )

        self._edit_theme_applied = False

    def _apply_color_reset_stylesheet(
        self,
        widget: QWidget,
        cached_stylesheet: str | None,
        selector: str,
    ) -> None:
        """Recombine *widget*'s cached stylesheet with a neutral text colour."""

        base_stylesheet = (cached_stylesheet or "").strip()
        reset_stylesheet = "\n".join(
            [
                f"{selector} {{",
                "    color: unset;",
                "}",
            ]
        )
        combined_stylesheet = "\n".join(
            part for part in (base_stylesheet, reset_stylesheet) if part
        )
        widget.setStyleSheet(combined_stylesheet)

    def _refresh_menu_styles(self) -> None:
        """Rebuild the frameless window manager's menu palette if available."""

        if self._window is None:
            return
        window_manager = getattr(self._window, "window_manager", None)
        if window_manager is None:
            return
        apply_styles = getattr(window_manager, "_apply_menu_styles", None)
        if not callable(apply_styles):
            return
        apply_styles()

    def get_shell_animation_colors(
        self, entering: bool
    ) -> tuple[RoundedWindowShell | None, QColor | None, QColor | None]:
        """Return the shell widget plus start/end colours for transition animations."""

        shell = self._rounded_window_shell
        if shell is None:
            return None, None, None
        if self._default_rounded_shell_palette is None:
            self._default_rounded_shell_palette = QPalette(shell.palette())
        base_palette = self._default_rounded_shell_palette or QPalette(shell.palette())
        light_shell_color = base_palette.color(QPalette.ColorRole.Window)
        dark_shell_color = QColor("#1C1C1E")
        if entering:
            return shell, light_shell_color, dark_shell_color
        return shell, dark_shell_color, light_shell_color
