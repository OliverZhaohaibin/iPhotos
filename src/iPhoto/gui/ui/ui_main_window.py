"""UI definition for the primary application window."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QMetaObject, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenuBar,
    QProgressBar,
    QPushButton,
    QSlider,
    QSizeGrip,
    QSizePolicy,
    QSpacerItem,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .icon import load_icon
from .palette import viewer_surface_color
from .widgets import (
    AlbumSidebar,
    FilmstripView,
    GalleryGridView,
    ImageViewer,
    EditSidebar,
    LiveBadge,
    PhotoMapView,
    PreviewWindow,
    VideoArea,
)
from .widgets.edit_topbar import SegmentedTopBar

HEADER_ICON_GLYPH_SIZE = QSize(24, 24)
"""Standard glyph size (in device-independent pixels) for header icons."""

HEADER_BUTTON_SIZE = QSize(36, 38)
"""Hit target size that guarantees a comfortable clickable header button."""

EDIT_HEADER_BUTTON_HEIGHT = HEADER_BUTTON_SIZE.height()
"""Uniform vertical extent for every interactive control in the edit toolbar."""

EDIT_DONE_BUTTON_BACKGROUND = "#FFD60A"
"""Primary accent colour that mirrors the Photos.app done button."""

EDIT_DONE_BUTTON_BACKGROUND_HOVER = "#FFE066"
"""Softer hover tint that preserves contrast against the yellow accent."""

EDIT_DONE_BUTTON_BACKGROUND_PRESSED = "#FFC300"
"""Darker pressed-state shade to communicate the button click."""

EDIT_DONE_BUTTON_BACKGROUND_DISABLED = "#CFC2A0"
"""Muted disabled colour that still reads as part of the yellow palette."""

EDIT_DONE_BUTTON_TEXT_COLOR = "#1C1C1E"
"""High-contrast foreground colour suitable for the yellow accent."""

EDIT_DONE_BUTTON_TEXT_DISABLED = "#7F7F7F"
"""Subdued text colour that keeps disabled labels legible."""

WINDOW_CONTROL_GLYPH_SIZE = QSize(16, 16)
"""Icon size used for the custom window chrome buttons."""

WINDOW_CONTROL_BUTTON_SIZE = QSize(26, 26)
"""Provides a reliable click target for the frameless window controls."""


class ChromeStatusBar(QWidget):
    """Lightweight status bar with an opaque background and progress indicator.

    The widget emulates the small subset of :class:`QStatusBar` behaviour that the
    controllers rely on (``showMessage``/``clearMessage``) while guaranteeing that the
    background remains fully opaque inside the rounded window shell.  Implementing a
    bespoke control avoids the transparency artefacts introduced by the native status bar
    when the main window uses ``Qt.WA_TranslucentBackground`` for anti-aliased corners.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chromeStatusBar")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)

        # Keep the status bar opaque even though the parent window uses
        # ``WA_TranslucentBackground``.  Copying the base colour into the Window
        # role ensures every style fills the background without inheriting
        # transparency from the palette.
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, palette.color(QPalette.ColorRole.Base))
        self.setPalette(palette)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)

        self._message_label = QLabel(self)
        self._message_label.setObjectName("statusMessageLabel")
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._message_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self._message_label, 1)

        self._progress_bar = self._create_progress_bar()
        layout.addWidget(self._progress_bar, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Reserve horizontal space so the floating resize indicator and size grip can occupy
        # the bottom-right corner without overlapping the progress bar.  A fixed-width spacer
        # keeps the layout intent explicit and makes future visual adjustments straightforward.
        resize_overlay_width = 25
        layout.addSpacerItem(
            QSpacerItem(
                resize_overlay_width,
                1,
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Minimum,
            )
        )

        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self.clearMessage)

    def _create_progress_bar(self) -> "QProgressBar":
        """Instantiate the determinate/indeterminate progress indicator.

        A dedicated helper keeps the constructor easy to read and ensures any future
        styling tweaks stay encapsulated in one place.
        """

        bar = QProgressBar(self)
        bar.setObjectName("statusProgress")
        bar.setVisible(False)
        bar.setMinimumWidth(160)
        bar.setTextVisible(False)
        bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return bar

    @property
    def progress_bar(self) -> "QProgressBar":
        """Expose the embedded progress bar for controllers that drive it."""

        return self._progress_bar

    def showMessage(self, message: str, timeout: int = 0) -> None:  # noqa: N802 - Qt-style API
        """Display a status message optionally cleared after ``timeout`` milliseconds."""

        self._message_label.setText(message)
        self._clear_timer.stop()
        if timeout > 0:
            self._clear_timer.start(max(0, timeout))

    def clearMessage(self) -> None:  # noqa: N802 - Qt-style API
        """Remove the current message and cancel any pending timeout."""

        self._message_label.clear()
        self._clear_timer.stop()

    def currentMessage(self) -> str:  # noqa: N802 - Qt-style API
        """Return the text currently shown in the status bar."""

        return self._message_label.text()

TITLE_BAR_HEIGHT = WINDOW_CONTROL_BUTTON_SIZE.height() + 16
"""Fixed vertical extent for the custom title bar including padding."""


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

        # ``window_shell`` hosts every visible surface so the rounded window chrome can keep the
        # menu bar, tool bar, and content area opaque while the corners remain transparent.
        self.window_shell = QWidget(MainWindow)
        self.window_shell_layout = QVBoxLayout(self.window_shell)
        self.window_shell_layout.setContentsMargins(0, 0, 0, 0)
        self.window_shell_layout.setSpacing(0)

        # Place a resize indicator as an overlay widget.  The label is created with the main
        # window as its initial parent so layout code does not immediately manage it; the real
        # parent is assigned once ``RoundedWindowShell`` exists inside ``MainWindow``.  The
        # overlay ignores mouse input to avoid blocking resize drags that originate from the
        # frameless window edge.
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

        # ``QSizeGrip`` is instantiated alongside the icon so the visual affordance can sit directly
        # above the interactive handle.  Assigning the temporary parent keeps the grip out of the
        # auto-generated layouts until the main window finishes wrapping the rounded shell.
        self.size_grip = QSizeGrip(MainWindow)
        self.size_grip.setObjectName("resizeSizeGrip")
        self.size_grip.setFixedSize(indicator_size)
        self.size_grip.hide()

        self.menu_bar = QMenuBar(self.window_shell)
        self.menu_bar.setObjectName("chromeMenuBar")
        # Hosting the menu bar inside the rounded shell keeps the chrome opaque while still
        # allowing macOS to fall back to the in-window variant instead of the application-wide
        # native menu.  This approach produces consistent visuals across platforms.
        self.menu_bar.setNativeMenuBar(False)
        self.menu_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.menu_bar.setAutoFillBackground(True)
        self.menu_bar.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        menu_palette = self.menu_bar.palette()
        menu_palette.setColor(
            QPalette.ColorRole.Window,
            menu_palette.color(QPalette.ColorRole.Base),
        )
        self.menu_bar.setPalette(menu_palette)

        # Collect the custom title bar and its separator inside a dedicated container so the
        # main window can hide or show the entire chrome strip with a single widget toggle when
        # entering or exiting immersive full screen mode.
        self.window_chrome = QWidget(self.window_shell)
        # Keep the chrome wrapper at a fixed height so the custom title bar and its controls do
        # not stretch vertically when the main window grows taller.  The horizontal policy stays
        # ``Preferred`` so the container can expand to match the available width.
        self.window_chrome.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        window_chrome_layout = QVBoxLayout(self.window_chrome)
        window_chrome_layout.setContentsMargins(0, 0, 0, 0)
        window_chrome_layout.setSpacing(0)

        # -------------------- Custom title bar --------------------
        self.title_bar = QWidget(self.window_chrome)
        self.title_bar.setObjectName("windowTitleBar")
        # Explicitly fix the title bar height and size policy to lock the macOS-style traffic
        # light controls in place regardless of how tall the application window becomes.
        self.title_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(12, 10, 12, 6)
        title_layout.setSpacing(8)
        self.title_bar.setFixedHeight(TITLE_BAR_HEIGHT)

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
        self.window_controls.setFixedHeight(WINDOW_CONTROL_BUTTON_SIZE.height())
        self.window_controls.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )

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
        window_chrome_layout.addWidget(self.title_bar)

        self.title_separator = QFrame(self.window_chrome)
        self.title_separator.setObjectName("windowTitleSeparator")
        self.title_separator.setFrameShape(QFrame.Shape.HLine)
        self.title_separator.setFrameShadow(QFrame.Shadow.Plain)
        self.title_separator.setFixedHeight(1)
        window_chrome_layout.addWidget(self.title_separator)

        self.window_shell_layout.addWidget(self.window_chrome)
        self.window_shell_layout.addWidget(self.menu_bar)

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

        self.main_toolbar = QToolBar("Main", self.window_shell)
        # Hosting the toolbar inside the rounded shell keeps the controls opaque while avoiding
        # the transparent gap that appeared when Qt painted it outside the custom chrome.
        self.main_toolbar.setObjectName("mainToolbar")
        self.main_toolbar.setMovable(False)
        self.main_toolbar.setFloatable(False)
        self.main_toolbar.setOrientation(Qt.Orientation.Horizontal)
        self.main_toolbar.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.main_toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.main_toolbar.setAutoFillBackground(True)
        toolbar_palette = self.main_toolbar.palette()
        toolbar_palette.setColor(
            QPalette.ColorRole.Window,
            toolbar_palette.color(QPalette.ColorRole.Base),
        )
        self.main_toolbar.setPalette(toolbar_palette)
        self.window_shell_layout.addWidget(self.main_toolbar)
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
        self.edit_button = QToolButton()
        self.edit_button.setEnabled(False)
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
        # Ensure the main content area paints an opaque surface even though the frameless
        # window shell relies on ``WA_TranslucentBackground`` for rounded corners.
        right_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right_panel.setAutoFillBackground(True)
        # Copy a bright neutral colour into the palette so every child widget (image
        # viewer, video placeholder, etc.) reads the same base tone for their window
        # surfaces.  This keeps the editing area aligned with the original light theme.
        light_content_palette = right_panel.palette()
        content_bg_color = QColor(Qt.GlobalColor.white)
        light_content_palette.setColor(QPalette.ColorRole.Window, content_bg_color)
        light_content_palette.setColor(QPalette.ColorRole.Base, content_bg_color)
        right_panel.setPalette(light_content_palette)
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
            (self.edit_button, "slider.horizontal.3.svg", "Edit"),
        ):
            self._configure_header_button(button, icon_name, tooltip)
            actions_layout.addWidget(button)

        # Store the layout references so the edit controller can temporarily move
        # the information and favourite buttons into the edit toolbar without
        # losing their original position within the detail header.
        self.detail_actions_layout = actions_layout
        self.detail_info_button_index = actions_layout.indexOf(self.info_button)
        self.detail_favorite_button_index = actions_layout.indexOf(self.favorite_button)

        # Place the zoom widget directly beside the back button so the control cluster
        # mirrors the macOS Photos layout (navigation on the far left, actions on the right).
        header_layout.addWidget(self.zoom_widget)
        self.zoom_widget.hide()
        header_layout.addWidget(info_container, 1)
        header_layout.addWidget(actions_container)
        self.detail_header_layout = header_layout
        self.detail_zoom_widget_index = header_layout.indexOf(self.zoom_widget)
        # Group the header widgets under a single container so the immersive mode only needs to
        # toggle one widget to hide every metadata chrome element above the viewer.
        detail_chrome_container = QWidget()
        detail_chrome_layout = QVBoxLayout(detail_chrome_container)
        detail_chrome_layout.setContentsMargins(0, 0, 0, 0)
        detail_chrome_layout.setSpacing(6)

        detail_chrome_layout.addWidget(header)
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
        detail_chrome_layout.addWidget(header_separator)
        self.detail_header_separator = header_separator

        detail_layout.addWidget(detail_chrome_container)
        self.detail_chrome_container = detail_chrome_container

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

        # Edit page ----------------------------------------------------
        self.edit_mode_group = QActionGroup(MainWindow)
        self.edit_mode_group.setExclusive(True)
        self.edit_adjust_action = QAction(MainWindow)
        self.edit_adjust_action.setCheckable(True)
        self.edit_adjust_action.setChecked(True)
        self.edit_mode_group.addAction(self.edit_adjust_action)
        self.edit_crop_action = QAction(MainWindow)
        self.edit_crop_action.setCheckable(True)
        self.edit_mode_group.addAction(self.edit_crop_action)

        self.edit_compare_button = QToolButton(MainWindow)
        self.edit_reset_button = QPushButton(MainWindow)
        self.edit_done_button = QPushButton(MainWindow)
        self.edit_image_viewer = ImageViewer()
        self.edit_sidebar = EditSidebar()
        self.edit_sidebar.setObjectName("editSidebar")
        # Capture the sidebar's default geometry constraints before temporarily collapsing it for
        # the animated transition.  Stashing these values as dynamic properties keeps them
        # accessible to the edit controller once the minimum/maximum widths are reduced to zero.
        default_sidebar_min = self.edit_sidebar.minimumWidth()
        default_sidebar_max = self.edit_sidebar.maximumWidth()
        default_sidebar_hint = max(self.edit_sidebar.sizeHint().width(), default_sidebar_min)
        self.edit_sidebar.setProperty("defaultMinimumWidth", default_sidebar_min)
        self.edit_sidebar.setProperty("defaultMaximumWidth", default_sidebar_max)
        self.edit_sidebar.setProperty("defaultPreferredWidth", default_sidebar_hint)
        # Start the edit sidebar hidden so the first switch into edit mode
        # can animate the panel sliding out instead of popping to its full
        # width immediately.
        self.edit_sidebar.setMinimumWidth(0)
        self.edit_sidebar.setMaximumWidth(0)
        self.edit_sidebar.hide()

        edit_page = QWidget()
        edit_page.setObjectName("editPage")
        edit_layout = QVBoxLayout(edit_page)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        edit_layout.setSpacing(6)

        edit_header_container = QWidget()
        edit_header_container.setObjectName("editHeaderContainer")
        edit_header_layout = QHBoxLayout(edit_header_container)
        edit_header_layout.setContentsMargins(12, 0, 12, 0)
        edit_header_layout.setSpacing(12)

        left_controls_container = QWidget(edit_header_container)
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
        # Matching the custom header button footprint keeps the compare control aligned with the
        # textual buttons while maintaining the larger icon hit target demanded by the spec.
        self.edit_compare_button.setFixedSize(HEADER_BUTTON_SIZE)
        left_controls_layout.addWidget(self.edit_compare_button)

        self.edit_reset_button.setAutoDefault(False)
        self.edit_reset_button.setDefault(False)
        self.edit_reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Constraining the height to the shared toolbar dimension ensures the text baseline lines
        # up with the adjacent segmented control and Done button.
        self.edit_reset_button.setFixedHeight(EDIT_HEADER_BUTTON_HEIGHT)
        left_controls_layout.addWidget(self.edit_reset_button)

        self.edit_zoom_host = QWidget(left_controls_container)
        self.edit_zoom_host_layout = QHBoxLayout(self.edit_zoom_host)
        self.edit_zoom_host_layout.setContentsMargins(0, 0, 0, 0)
        self.edit_zoom_host_layout.setSpacing(4)
        left_controls_layout.addWidget(self.edit_zoom_host)

        edit_header_layout.addWidget(left_controls_container)

        self.edit_mode_control = SegmentedTopBar(
            (
                self.edit_adjust_action.text() or "Adjust",
                self.edit_crop_action.text() or "Crop",
            ),
            edit_header_container,
        )
        edit_header_layout.addWidget(self.edit_mode_control, 0, Qt.AlignmentFlag.AlignHCenter)

        right_controls_container = QWidget(edit_header_container)
        right_controls_layout = QHBoxLayout(right_controls_container)
        right_controls_layout.setContentsMargins(0, 0, 0, 0)
        right_controls_layout.setSpacing(8)
        right_controls_container.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Preferred,
        )
        # A dedicated object name keeps the stylesheet scoped to this button while allowing
        # the controller to continue referencing ``edit_done_button`` for signal wiring.
        self.edit_done_button.setObjectName("editDoneButton")
        self.edit_done_button.setAutoDefault(False)
        self.edit_done_button.setDefault(False)
        self.edit_done_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_done_button.setFixedHeight(EDIT_HEADER_BUTTON_HEIGHT)
        # The button colour replicates the warm yellow accent from the reference design while
        # providing hover/pressed variations so the control still communicates interaction.
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
        edit_header_layout.addWidget(right_controls_container)

        # Preserve a handle to the layout so the edit controller can move detail
        # action buttons (info and favourite) into this container while editing.
        self.edit_right_controls_layout = right_controls_layout

        edit_layout.addWidget(edit_header_container)

        edit_body = QWidget(edit_page)
        edit_body_layout = QHBoxLayout(edit_body)
        edit_body_layout.setContentsMargins(0, 0, 0, 0)
        edit_body_layout.setSpacing(12)
        edit_body_layout.addWidget(self.edit_image_viewer, 1)
        edit_body_layout.addWidget(self.edit_sidebar)
        edit_layout.addWidget(edit_body, 1)

        self.edit_header_container = edit_header_container
        self.edit_page = edit_page
        self.edit_header_container.hide()

        self.edit_mode_control.setCurrentIndex(0, animate=False)

        self.view_stack.addWidget(self.edit_page)

        self.view_stack.setCurrentWidget(self.gallery_page)
        right_layout.addWidget(self.view_stack)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        # Allow the album sidebar (the first splitter pane) to collapse so the
        # edit-mode animation can drive its width to zero without fighting the
        # splitter's built-in constraints.  The second pane remains fixed to
        # preserve the main content area's minimum footprint.
        self.splitter.setCollapsible(0, True)
        self.splitter.setCollapsible(1, False)

        self.window_shell_layout.addWidget(self.splitter)

        self.status_bar = ChromeStatusBar(self.window_shell)
        self.window_shell_layout.addWidget(self.status_bar)
        self.progress_bar = self.status_bar.progress_bar

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

