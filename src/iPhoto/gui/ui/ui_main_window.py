"""UI definition for the primary application window."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QMetaObject, QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QMainWindow,
    QSizeGrip,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .icon import load_icon
from .widgets import (
    AlbumSidebar,
    ChromeStatusBar,
    CustomTitleBar,
    DetailPageWidget,
    EditPageWidget,
    GalleryPageWidget,
    MainHeaderWidget,
    PhotoMapView,
    PreviewWindow,
)


class Ui_MainWindow(object):
    """Pure UI layer for :class:`~PySide6.QtWidgets.QMainWindow`."""

    def setupUi(self, MainWindow: QMainWindow, library) -> None:  # noqa: N802 - Qt style
        """Instantiate and lay out every widget composing the main window."""

        if not MainWindow.objectName():
            MainWindow.setObjectName("MainWindow")

        MainWindow.resize(1200, 720)

        self.window_shell = QWidget(MainWindow)
        self.window_shell_layout = QVBoxLayout(self.window_shell)
        self.window_shell_layout.setContentsMargins(0, 0, 0, 0)
        self.window_shell_layout.setSpacing(0)

        self.resize_indicator = QLabel(MainWindow)
        self.resize_indicator.setObjectName("resizeIndicatorLabel")
        indicator_size = QSize(20, 20)
        self.resize_indicator.setFixedSize(indicator_size)
        self.resize_indicator.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.resize_indicator.setScaledContents(True)
        self.resize_indicator.setPixmap(load_icon("resize.svg").pixmap(indicator_size))
        self.resize_indicator.hide()

        self.size_grip = QSizeGrip(MainWindow)
        self.size_grip.setObjectName("resizeSizeGrip")
        self.size_grip.setFixedSize(indicator_size)
        self.size_grip.hide()

        self.window_chrome = QWidget(self.window_shell)
        self.window_chrome.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        window_chrome_layout = QVBoxLayout(self.window_chrome)
        window_chrome_layout.setContentsMargins(0, 0, 0, 0)
        window_chrome_layout.setSpacing(0)

        self.title_bar = CustomTitleBar(self.window_chrome, MainWindow.windowTitle())
        self.window_title_label = self.title_bar.window_title_label
        self.window_controls = self.title_bar.window_controls
        self.minimize_button = self.title_bar.minimize_button
        self.fullscreen_button = self.title_bar.fullscreen_button
        self.close_button = self.title_bar.close_button
        window_chrome_layout.addWidget(self.title_bar)

        self.title_separator = QFrame(self.window_chrome)
        self.title_separator.setObjectName("windowTitleSeparator")
        self.title_separator.setFrameShape(QFrame.Shape.HLine)
        self.title_separator.setFrameShadow(QFrame.Shadow.Plain)
        self.title_separator.setFixedHeight(1)
        window_chrome_layout.addWidget(self.title_separator)

        self.main_header = MainHeaderWidget(self.window_shell, MainWindow)
        self.menu_bar_container = self.main_header
        self.menu_bar = self.main_header.menu_bar
        self.rescan_button = self.main_header.rescan_button
        self.selection_button = self.main_header.selection_button
        self.open_album_action = self.main_header.open_album_action
        self.rescan_action = self.main_header.rescan_action
        self.rebuild_links_action = self.main_header.rebuild_links_action
        self.bind_library_action = self.main_header.bind_library_action
        self.toggle_filmstrip_action = self.main_header.toggle_filmstrip_action
        self.share_action_group = self.main_header.share_action_group
        self.share_action_copy_file = self.main_header.share_action_copy_file
        self.share_action_copy_path = self.main_header.share_action_copy_path
        self.share_action_reveal_file = self.main_header.share_action_reveal_file
        self.wheel_action_group = self.main_header.wheel_action_group
        self.wheel_action_navigate = self.main_header.wheel_action_navigate
        self.wheel_action_zoom = self.main_header.wheel_action_zoom

        self.window_shell_layout.addWidget(self.window_chrome)
        self.window_shell_layout.addWidget(self.menu_bar_container)

        self.sidebar = AlbumSidebar(library, MainWindow)
        self.preview_window = PreviewWindow(MainWindow)
        self.map_view = PhotoMapView()

        self.gallery_page = GalleryPageWidget()
        self.grid_view = self.gallery_page.grid_view

        self.detail_page = DetailPageWidget(MainWindow)
        self.back_button = self.detail_page.back_button
        self.info_button = self.detail_page.info_button
        self.share_button = self.detail_page.share_button
        self.favorite_button = self.detail_page.favorite_button
        self.edit_button = self.detail_page.edit_button
        self.zoom_widget = self.detail_page.zoom_widget
        self.zoom_slider = self.detail_page.zoom_slider
        self.zoom_in_button = self.detail_page.zoom_in_button
        self.zoom_out_button = self.detail_page.zoom_out_button
        self.location_label = self.detail_page.location_label
        self.timestamp_label = self.detail_page.timestamp_label
        self.detail_actions_layout = self.detail_page.detail_actions_layout
        self.detail_info_button_index = self.detail_page.detail_info_button_index
        self.detail_favorite_button_index = self.detail_page.detail_favorite_button_index
        self.detail_header_layout = self.detail_page.detail_header_layout
        self.detail_zoom_widget_index = self.detail_page.detail_zoom_widget_index
        self.detail_header = self.detail_page.detail_header
        self.detail_chrome_container = self.detail_page.detail_chrome_container
        self.detail_header_separator = self.detail_page.detail_header_separator
        self.player_stack = self.detail_page.player_stack
        self.player_placeholder = self.detail_page.player_placeholder
        self.image_viewer = self.detail_page.image_viewer
        self.video_area = self.detail_page.video_area
        self.player_bar = self.detail_page.player_bar
        self.filmstrip_view = self.detail_page.filmstrip_view
        self.live_badge = self.detail_page.live_badge
        self.badge_host = self.detail_page.badge_host
        self.player_container = self.detail_page.player_container

        self.edit_page = EditPageWidget(MainWindow)
        self.edit_mode_group = self.edit_page.edit_mode_group
        self.edit_adjust_action = self.edit_page.edit_adjust_action
        self.edit_crop_action = self.edit_page.edit_crop_action
        self.edit_compare_button = self.edit_page.edit_compare_button
        self.edit_reset_button = self.edit_page.edit_reset_button
        self.edit_done_button = self.edit_page.edit_done_button
        self.edit_image_viewer = self.edit_page.edit_image_viewer
        self.edit_sidebar = self.edit_page.edit_sidebar
        self.edit_mode_control = self.edit_page.edit_mode_control
        self.edit_header_container = self.edit_page.edit_header_container
        self.edit_zoom_host = self.edit_page.edit_zoom_host
        self.edit_zoom_host_layout = self.edit_page.edit_zoom_host_layout
        self.edit_right_controls_layout = self.edit_page.edit_right_controls_layout

        right_panel = QWidget()
        right_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right_panel.setAutoFillBackground(True)
        light_content_palette = right_panel.palette()
        content_bg_color = QColor(Qt.GlobalColor.white)
        light_content_palette.setColor(QPalette.ColorRole.Window, content_bg_color)
        light_content_palette.setColor(QPalette.ColorRole.Base, content_bg_color)
        right_panel.setPalette(light_content_palette)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)

        self.view_stack = QStackedWidget()
        map_page = QWidget()
        map_layout = QVBoxLayout(map_page)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.setSpacing(0)
        map_layout.addWidget(self.map_view)
        self.map_page = map_page

        self.view_stack.addWidget(self.gallery_page)
        self.view_stack.addWidget(self.map_page)
        self.view_stack.addWidget(self.detail_page)
        self.view_stack.addWidget(self.edit_page)
        self.view_stack.setCurrentWidget(self.gallery_page)
        right_layout.addWidget(self.view_stack)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(0, True)
        self.splitter.setCollapsible(1, False)

        self.window_shell_layout.addWidget(self.splitter)

        self.status_bar = ChromeStatusBar(self.window_shell)
        self.window_shell_layout.addWidget(self.status_bar)
        self.progress_bar = self.status_bar.progress_bar

        MainWindow.setCentralWidget(self.window_shell)

        if self.player_container is not None:
            self.player_container.installEventFilter(MainWindow)

        self.retranslateUi(MainWindow)
        QMetaObject.connectSlotsByName(MainWindow)

    def retranslateUi(self, MainWindow: QMainWindow) -> None:  # noqa: N802 - Qt style
        """Apply translatable strings to the window."""

        MainWindow.setWindowTitle(
            QCoreApplication.translate("MainWindow", "iPhoto", None)
        )
        self.window_title_label.setText(MainWindow.windowTitle())
        self.minimize_button.setToolTip(
            QCoreApplication.translate("MainWindow", "Minimize", None)
        )
        self.fullscreen_button.setToolTip(
            QCoreApplication.translate("MainWindow", "Enter Full Screen", None)
        )
        self.close_button.setToolTip(
            QCoreApplication.translate("MainWindow", "Close", None)
        )
        self.selection_button.setText(
            QCoreApplication.translate("MainWindow", "Select", None)
        )
        self.selection_button.setToolTip(
            QCoreApplication.translate(
                "MainWindow",
                "Toggle multi-selection mode",
                None,
            )
        )
        self.edit_adjust_action.setText(
            QCoreApplication.translate("MainWindow", "Adjust", None)
        )
        self.edit_crop_action.setText(
            QCoreApplication.translate("MainWindow", "Crop", None)
        )
        self.edit_mode_control.setItems(
            (
                self.edit_adjust_action.text(),
                self.edit_crop_action.text(),
            )
        )
        self.edit_compare_button.setToolTip(
            QCoreApplication.translate(
                "MainWindow",
                "Press and hold to preview the unedited photo",
                None,
            )
        )
        self.edit_reset_button.setText(
            QCoreApplication.translate("MainWindow", "Revert to Original", None)
        )
        self.edit_reset_button.setToolTip(
            QCoreApplication.translate(
                "MainWindow",
                "Restore every adjustment to its original value",
                None,
            )
        )
        self.edit_done_button.setText(
            QCoreApplication.translate("MainWindow", "Done", None)
        )
        self.open_album_action.setText(
            QCoreApplication.translate("MainWindow", "Open Album Folder…", None)
        )
        self.rescan_action.setText(
            QCoreApplication.translate("MainWindow", "Rescan", None)
        )
        self.rebuild_links_action.setText(
            QCoreApplication.translate("MainWindow", "Rebuild Live Links", None)
        )
        self.bind_library_action.setText(
            QCoreApplication.translate("MainWindow", "Set Basic Library…", None)
        )
        self.toggle_filmstrip_action.setText(
            QCoreApplication.translate("MainWindow", "Show Filmstrip", None)
        )
        self.share_action_copy_file.setText(
            QCoreApplication.translate("MainWindow", "Copy File", None)
        )
        self.share_action_copy_path.setText(
            QCoreApplication.translate("MainWindow", "Copy Path", None)
        )
        self.share_action_reveal_file.setText(
            QCoreApplication.translate("MainWindow", "Reveal in File Manager", None)
        )
        self.wheel_action_navigate.setText(
            QCoreApplication.translate("MainWindow", "Navigate", None)
        )
        self.wheel_action_zoom.setText(
            QCoreApplication.translate("MainWindow", "Zoom", None)
        )


__all__ = ["Ui_MainWindow"]
