"""UI definition for the primary application window."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QMetaObject, QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenuBar,
    QProgressBar,
    QSlider,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .icons import load_icon
from .palette import viewer_surface_color
from .widgets import (
    AlbumSidebar,
    FilmstripView,
    GalleryGridView,
    ImageViewer,
    LiveBadge,
    PhotoMapView,
    PreviewWindow,
    VideoArea,
)

HEADER_ICON_GLYPH_SIZE = QSize(24, 24)
"""Standard glyph size (in device-independent pixels) for header icons."""

HEADER_BUTTON_SIZE = QSize(36, 38)
"""Hit target size that guarantees a comfortable clickable header button."""

WINDOW_CONTROL_GLYPH_SIZE = QSize(16, 16)
"""Icon size used for the custom window chrome buttons."""

WINDOW_CONTROL_BUTTON_SIZE = QSize(26, 26)
"""Provides a reliable click target for the frameless window controls."""


class Ui_MainWindow(object):
    """Pure UI layer for :class:`~PySide6.QtWidgets.QMainWindow`.

    The class mirrors the structure produced by ``pyuic`` so the widget
    hierarchy lives in a single, importable module.  All widgets are exposed as
    public attributes so controllers can reference them without forcing the
    view to know about application logic.
    """

    def setupUi(self, MainWindow: QMainWindow, library) -> None:  # noqa: N802 - Qt style
        """Instantiate and lay out every widget composing the main window.

        Parameters
        ----------
        MainWindow:
            The concrete :class:`QMainWindow` receiving the widgets.
        library:
            The album library descriptor used by :class:`AlbumSidebar` to
            populate its tree.  The concrete type is deliberately left open
            because the sidebar only requires a duck-typed interface.
        """

        if not MainWindow.objectName():
            MainWindow.setObjectName("MainWindow")

        MainWindow.resize(1200, 720)

        self.menu_bar = QMenuBar(MainWindow)
        MainWindow.setMenuBar(self.menu_bar)

        self.status_bar = QStatusBar(MainWindow)
        MainWindow.setStatusBar(self.status_bar)

        self.progress_bar = QProgressBar(MainWindow)
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumWidth(160)
        self.progress_bar.setTextVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)

        # Assemble the frameless host widget that replaces the native window chrome. All
        # content — including the custom title bar — lives inside this container so the
        # window can hide everything quickly when immersive full screen mode is activated.
        self.window_shell = QWidget(MainWindow)
        self.window_shell_layout = QVBoxLayout(self.window_shell)
        self.window_shell_layout.setContentsMargins(0, 0, 0, 0)
        self.window_shell_layout.setSpacing(0)

        # -------------------- Custom title bar --------------------
        self.title_bar = QWidget(self.window_shell)
        self.title_bar.setObjectName("windowTitleBar")
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(12, 10, 12, 6)
        title_layout.setSpacing(8)

        self.window_title_label = QLabel(MainWindow.windowTitle())
        self.window_title_label.setObjectName("windowTitleLabel")
        self.window_title_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.window_title_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        title_layout.addWidget(self.window_title_label, 1)

        self.window_controls = QWidget(self.title_bar)
        self.window_controls.setObjectName("windowControls")
        controls_layout = QHBoxLayout(self.window_controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)

        self.minimize_button = QToolButton(self.window_controls)
        self.fullscreen_button = QToolButton(self.window_controls)
        self.close_button = QToolButton(self.window_controls)

        for button, icon_name, tooltip in (
            (self.minimize_button, "yellow.minimum.circle.svg", "Minimize"),
            (self.fullscreen_button, "green.maximum.circle.svg", "Enter Full Screen"),
            (self.close_button, "red.close.circle.svg", "Close"),
        ):
            self._configure_window_control_button(button, icon_name, tooltip)
            controls_layout.addWidget(button)

        title_layout.addWidget(self.window_controls, 0, Qt.AlignmentFlag.AlignRight)
        self.window_shell_layout.addWidget(self.title_bar)

        self.title_separator = QFrame(self.window_shell)
        self.title_separator.setObjectName("windowTitleSeparator")
        self.title_separator.setFrameShape(QFrame.Shape.HLine)
        self.title_separator.setFrameShadow(QFrame.Shadow.Plain)
        self.title_separator.setFixedHeight(1)
        self.window_shell_layout.addWidget(self.title_separator)

        self.open_album_action = QAction("Open Album Folder…", MainWindow)
        self.rescan_action = QAction("Rescan", MainWindow)
        self.rebuild_links_action = QAction("Rebuild Live Links", MainWindow)
        self.bind_library_action = QAction("Set Basic Library…", MainWindow)
        # Provide a persistent toggle so users can hide the filmstrip when they want to focus
        # solely on the large preview while still being able to restore it later.
        self.toggle_filmstrip_action = QAction("Show Filmstrip", MainWindow, checkable=True)
        self.toggle_filmstrip_action.setChecked(True)

        # Group share actions so only one preferred behaviour can be active at a time.
        self.share_action_group = QActionGroup(MainWindow)
        self.share_action_copy_file = QAction("Copy File", MainWindow, checkable=True)
        self.share_action_copy_path = QAction("Copy Path", MainWindow, checkable=True)
        self.share_action_reveal_file = QAction(
            "Reveal in File Manager", MainWindow, checkable=True
        )
        self.share_action_group.addAction(self.share_action_copy_file)
        self.share_action_group.addAction(self.share_action_copy_path)
        self.share_action_group.addAction(self.share_action_reveal_file)
        self.share_action_reveal_file.setChecked(True)

        # The wheel action group mirrors the share action group: it lets the user pick a single
        # behaviour that will be mirrored across the viewer and filmstrip. Keeping the actions in
        # a dedicated group guarantees that only one option can be checked at a time and the UI can
        # simply inspect ``checkedAction`` when persisting the preference.
        self.wheel_action_group = QActionGroup(MainWindow)
        self.wheel_action_navigate = QAction("Navigate", MainWindow, checkable=True)
        self.wheel_action_zoom = QAction("Zoom", MainWindow, checkable=True)
        self.wheel_action_group.addAction(self.wheel_action_navigate)
        self.wheel_action_group.addAction(self.wheel_action_zoom)
        self.wheel_action_navigate.setChecked(True)

        file_menu = self.menu_bar.addMenu("&File")
        for action in (
            self.open_album_action,
            None,
            self.bind_library_action,
            None,
            self.rescan_action,
            self.rebuild_links_action,
        ):
            if action is None:
                file_menu.addSeparator()
            else:
                file_menu.addAction(action)

        self.main_toolbar = QToolBar("Main", MainWindow)
        self.main_toolbar.setMovable(False)
        MainWindow.addToolBar(self.main_toolbar)
        for action in (
            self.open_album_action,
            self.rescan_action,
            self.rebuild_links_action,
        ):
            self.main_toolbar.addAction(action)

        settings_menu = self.menu_bar.addMenu("&Settings")
        # Reuse the same action instance in both menus so the user can discover the
        # library binding workflow from either File or Settings without duplicating
        # business logic or state handling.
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

        self.sidebar = AlbumSidebar(library, MainWindow)
        self.album_label = QLabel("Open a folder to browse your photos.")
        self.grid_view = GalleryGridView()
        self.map_view = PhotoMapView()
        self.filmstrip_view = FilmstripView()
        self.video_area = VideoArea()
        self.player_bar = self.video_area.player_bar
        self.preview_window = PreviewWindow(MainWindow)
        self.image_viewer = ImageViewer()
        self.player_placeholder = QLabel("Select a photo or video to preview.")
        self.player_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Present the placeholder on the shared viewer surface colour so the transition
        # to actual photo or video content feels seamless and visually cohesive.
        # Delegate both the background and text colour to the active palette so the
        # placeholder looks indistinguishable from the fully rendered viewer.
        self.player_placeholder.setStyleSheet(
            "background-color: palette(window); "
            "color: palette(window-text); font-size: 16px;"
        )
        self.player_placeholder.setMinimumHeight(320)
        self.player_stack = QStackedWidget()
        self.view_stack = QStackedWidget()

        self.back_button = QToolButton()
        self.info_button = QToolButton()
        self.share_button = QToolButton()
        self.favorite_button = QToolButton()
        self.favorite_button.setEnabled(False)
        self.zoom_widget = QWidget()
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_in_button = QToolButton()
        self.zoom_out_button = QToolButton()

        self.live_badge = LiveBadge(MainWindow)
        self.live_badge.hide()
        self.badge_host: QWidget | None = None

        self.location_label = QLabel()
        self.timestamp_label = QLabel()

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)

        self.album_label.setObjectName("albumLabel")
        album_header = QWidget()
        album_header_layout = QHBoxLayout(album_header)
        album_header_layout.setContentsMargins(0, 0, 0, 0)
        album_header_layout.setSpacing(8)
        album_header_layout.addWidget(self.album_label, 1)
        self.selection_button = QToolButton()
        self.selection_button.setObjectName("selectionButton")
        self.selection_button.setAutoRaise(True)
        self.selection_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        album_header_layout.addWidget(self.selection_button)
        right_layout.addWidget(album_header)
        self.album_header = album_header

        gallery_page = QWidget()
        gallery_layout = QVBoxLayout(gallery_page)
        gallery_layout.setContentsMargins(0, 0, 0, 0)
        gallery_layout.setSpacing(0)
        gallery_layout.addWidget(self.grid_view)
        self.gallery_page = gallery_page

        map_page = QWidget()
        map_layout = QVBoxLayout(map_page)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.setSpacing(0)
        map_layout.addWidget(self.map_view)
        self.map_page = map_page

        detail_page = QWidget()
        detail_layout = QVBoxLayout(detail_page)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 12, 0)
        header_layout.setSpacing(8)

        self._configure_header_button(
            self.back_button,
            "chevron.left.svg",
            "Return to grid view",
        )
        header_layout.addWidget(self.back_button)

        info_container = QWidget()
        info_layout = QVBoxLayout(info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        info_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        base_font = MainWindow.font()

        location_font = QFont(base_font)
        if location_font.pointSize() > 0:
            location_font.setPointSize(location_font.pointSize() + 2)
        else:
            location_font.setPointSize(14)
        location_font.setBold(True)

        timestamp_font = QFont(base_font)
        if timestamp_font.pointSize() > 0:
            timestamp_font.setPointSize(max(timestamp_font.pointSize() + 1, 1))
        else:
            timestamp_font.setPointSize(12)
        timestamp_font.setBold(False)

        self.location_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.location_label.setFont(location_font)
        self.location_label.setVisible(False)

        self.timestamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timestamp_label.setFont(timestamp_font)
        self.timestamp_label.setVisible(False)

        info_layout.addWidget(self.location_label)
        info_layout.addWidget(self.timestamp_label)

        zoom_layout = QHBoxLayout(self.zoom_widget)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(4)

        # Use compact controls so the zoom widget visually aligns with the action buttons.
        small_button_size = QSize(
            int(HEADER_BUTTON_SIZE.width() / 2),
            int(HEADER_BUTTON_SIZE.height() / 2),
        )

        self._configure_header_button(self.zoom_out_button, "minus.svg", "Zoom Out")
        self.zoom_out_button.setFixedSize(small_button_size)
        zoom_layout.addWidget(self.zoom_out_button)

        self.zoom_slider.setRange(10, 400)
        self.zoom_slider.setSingleStep(5)
        self.zoom_slider.setPageStep(25)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(90)
        self.zoom_slider.setToolTip("Zoom")
        zoom_layout.addWidget(self.zoom_slider)

        self._configure_header_button(self.zoom_in_button, "plus.svg", "Zoom In")
        self.zoom_in_button.setFixedSize(small_button_size)
        zoom_layout.addWidget(self.zoom_in_button)

        actions_container = QWidget()
        actions_layout = QHBoxLayout(actions_container)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)

        for button, icon_name, tooltip in (
            (self.info_button, "info.circle.svg", "Info"),
            (self.share_button, "square.and.arrow.up.svg", "Share"),
            (self.favorite_button, "suit.heart.svg", "Add to Favorites"),
        ):
            self._configure_header_button(button, icon_name, tooltip)
            actions_layout.addWidget(button)

        # Place the zoom widget directly beside the back button so the control cluster
        # mirrors the macOS Photos layout (navigation on the far left, actions on the right).
        header_layout.addWidget(self.zoom_widget)
        self.zoom_widget.hide()
        header_layout.addWidget(info_container, 1)
        header_layout.addWidget(actions_container)
        detail_layout.addWidget(header)
        self.detail_header = header

        # Insert a custom separator between the metadata header and the media area.
        # A soft drop shadow coupled with a light-toned line delivers the requested
        # subtle depth cue without overwhelming the surrounding chrome.
        header_separator = QFrame()
        header_separator.setObjectName("detailHeaderSeparator")
        header_separator.setFrameShape(QFrame.Shape.HLine)
        header_separator.setFrameShadow(QFrame.Shadow.Plain)
        header_separator.setFixedHeight(2)
        # Sample the surrounding detail page palette so the separator tint is
        # derived from the exact window background rather than a guessed hex
        # value.  This keeps the subtle depth cue consistent across styles.
        base_surface = viewer_surface_color(detail_page)
        separator_tint = QColor(base_surface).darker(108)
        header_separator.setStyleSheet(
            "QFrame#detailHeaderSeparator {"
            f"  background-color: {separator_tint.name()};"
            "  border: none;"
            "}"
        )
        separator_shadow = QGraphicsDropShadowEffect(header_separator)
        separator_shadow.setBlurRadius(14)
        separator_shadow.setColor(QColor(0, 0, 0, 45))
        separator_shadow.setOffset(0, 1)
        header_separator.setGraphicsEffect(separator_shadow)
        detail_layout.addWidget(header_separator)
        self.detail_header_separator = header_separator

        player_container = QWidget()
        player_layout = QVBoxLayout(player_container)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.setSpacing(0)
        player_layout.addWidget(self.player_stack)
        detail_layout.addWidget(player_container)
        self.player_container = player_container
        detail_layout.addWidget(self.filmstrip_view)
        self.detail_page = detail_page

        self.player_stack.addWidget(self.player_placeholder)
        self.player_stack.addWidget(self.image_viewer)
        self.player_stack.addWidget(self.video_area)
        self.player_stack.setCurrentWidget(self.player_placeholder)

        self.live_badge.setParent(player_container)
        self.badge_host = player_container
        self.live_badge.raise_()

        self.view_stack.addWidget(self.gallery_page)
        self.view_stack.addWidget(self.map_page)
        self.view_stack.addWidget(self.detail_page)
        self.view_stack.setCurrentWidget(self.gallery_page)
        right_layout.addWidget(self.view_stack)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)

        self.window_shell_layout.addWidget(self.splitter)

        MainWindow.setCentralWidget(self.window_shell)

        player_container.installEventFilter(MainWindow)

        self.retranslateUi(MainWindow)
        QMetaObject.connectSlotsByName(MainWindow)

    def _configure_header_button(
        self,
        button: QToolButton,
        icon_name: str,
        tooltip: str,
    ) -> None:
        """Normalize header button appearance to the design defaults."""

        button.setIcon(load_icon(icon_name))
        button.setIconSize(HEADER_ICON_GLYPH_SIZE)
        button.setFixedSize(HEADER_BUTTON_SIZE)
        button.setAutoRaise(True)

    def _configure_window_control_button(
        self,
        button: QToolButton,
        icon_name: str,
        tooltip: str,
    ) -> None:
        """Apply the shared styling for the custom window control buttons."""

        button.setIcon(load_icon(icon_name))
        button.setIconSize(WINDOW_CONTROL_GLYPH_SIZE)
        button.setFixedSize(WINDOW_CONTROL_BUTTON_SIZE)
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setToolTip(tooltip)

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

