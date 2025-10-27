"""Qt widgets composing the main application window."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, cast

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, QObject, QRectF, QRect
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMenuBar,
    QPlainTextEdit,
    QTextEdit,
    QVBoxLayout,
    QScrollBar,
    QWidget,
    QProxyStyle,
    QStyle,
    QStyleOptionComplex,
    QStyleOptionSlider,
)

# ``main_window`` can be imported either via ``iPhoto.gui`` (package execution)
# or ``iPhotos.src.iPhoto.gui`` (legacy test harness).  The absolute import
# keeps script-mode launches working where the relative form lacks package
# context.
try:  # pragma: no cover - exercised in packaging scenarios
    from ...appctx import AppContext
except ImportError:  # pragma: no cover - script execution fallback
    from iPhotos.src.iPhoto.appctx import AppContext

from ...config import VOLUME_SHORTCUT_STEP

from .controllers.main_controller import MainController
from .controllers.playback_state_manager import PlayerState
from .media import require_multimedia
from .ui_main_window import ChromeStatusBar, Ui_MainWindow
from .icon import load_icon
from .widgets.custom_tooltip import FloatingToolTip, ToolTipEventFilter


# Small delay that gives Qt time to settle window transitions before resuming playback.
PLAYBACK_RESUME_DELAY_MS = 120

# Property key used to mark ``QMenu`` widgets that already have the about-to-show styling hook
# attached.  Storing the marker directly on the widget avoids fragile bookkeeping structures and
# lets Qt manage the lifecycle in tandem with the menu itself.
_MENU_STYLE_PROPERTY = "_lexiphoto_menu_style_hook"


class FluentScrollbarStyle(QProxyStyle):
    """Proxy style that mimics the Fluent QSS scrollbars without incurring stylesheet cost."""

    _target_extent = 12
    _minimum_slider_length = 24
    _track_margin = 4
    _handle_radius = 6

    def __init__(self, base_style: QStyle) -> None:
        """Wrap ``base_style`` so only scrollbar painting is customised."""

        super().__init__(base_style)

    @staticmethod
    def _with_alpha(color: QColor, alpha: int) -> QColor:
        """Return ``color`` with its alpha channel replaced by ``alpha``."""

        constrained_alpha = max(0, min(255, int(alpha)))
        if color.alpha() == constrained_alpha:
            return QColor(color)
        adjusted = QColor(color)
        adjusted.setAlpha(constrained_alpha)
        return adjusted

    def pixelMetric(
        self,
        metric: QStyle.PixelMetric,
        option: QStyleOptionComplex | None,
        widget: QWidget | None,
    ) -> int:
        """Match the thickness and minimum length defined in the Fluent stylesheet."""

        base_value = super().pixelMetric(metric, option, widget)

        if metric == QStyle.PixelMetric.PM_ScrollBarExtent:
            # Keep the control exactly 12 px thick so it floats above the content the same way the
            # stylesheet-based version did, falling back to the base style only if it requests a
            # larger footprint (which can happen on accessibility-oriented styles).
            return max(self._target_extent, base_value)

        if metric == QStyle.PixelMetric.PM_ScrollBarSliderMin:
            # The previous QSS declared ``min-height``/``min-width`` of 24 px.  Preserving that
            # value keeps the drag handle easy to grab while still allowing smaller thumbs when Qt
            # needs to represent large ranges.
            return max(self._minimum_slider_length, base_value)

        return base_value

    def drawComplexControl(
        self,
        control: QStyle.ComplexControl,
        option: QStyleOptionComplex,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        """Render the translucent Fluent handle while leaving the track transparent."""

        if control != QStyle.ComplexControl.CC_ScrollBar:
            super().drawComplexControl(control, option, painter, widget)
            return

        slider_option = cast(QStyleOptionSlider, option)
        if not slider_option.subControls & QStyle.SubControl.SC_ScrollBarSlider:
            # No slider to paint, so fall back to the base implementation.  This scenario is rare
            # but can occur while Qt computes transient geometries during hover transitions.
            super().drawComplexControl(control, option, painter, widget)
            return

        handle_rect = self.subControlRect(
            control,
            slider_option,
            QStyle.SubControl.SC_ScrollBarSlider,
            widget,
        )

        if not handle_rect.isValid() or handle_rect.isEmpty():
            return

        state = slider_option.state
        palette = slider_option.palette

        mid_tone = palette.color(QPalette.ColorRole.Mid)
        accent = palette.color(QPalette.ColorRole.Highlight)

        if not state & QStyle.StateFlag.State_Enabled:
            fill_color = self._with_alpha(mid_tone, 70)
        elif state & QStyle.StateFlag.State_Sunken:
            fill_color = self._with_alpha(accent, 255)
        elif state & QStyle.StateFlag.State_MouseOver:
            fill_color = self._with_alpha(accent, 200)
        else:
            fill_color = self._with_alpha(mid_tone, 140)

        radius = min(self._handle_radius, min(handle_rect.width(), handle_rect.height()) / 2)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill_color)
        painter.drawRoundedRect(QRectF(handle_rect), radius, radius)
        painter.restore()

    def subControlRect(
        self,
        control: QStyle.ComplexControl,
        option: QStyleOptionComplex,
        sub_control: QStyle.SubControl,
        widget: QWidget | None = None,
    ) -> QRect:
        """Collapse arrow buttons and inject the 4 px margins from the Fluent stylesheet."""

        rect = super().subControlRect(control, option, sub_control, widget)
        if control != QStyle.ComplexControl.CC_ScrollBar:
            return rect

        if sub_control in {
            QStyle.SubControl.SC_ScrollBarAddLine,
            QStyle.SubControl.SC_ScrollBarSubLine,
            QStyle.SubControl.SC_ScrollBarFirst,
            QStyle.SubControl.SC_ScrollBarLast,
        }:
            # The Fluent design omits dedicated arrow buttons in favour of a clean track.
            # Returning an empty rect tells Qt to skip painting those controls while still
            # leaving page-step interactions intact.
            return QRect()

        slider_option = cast(QStyleOptionSlider, option)

        if sub_control == QStyle.SubControl.SC_ScrollBarGroove:
            groove = QRect(rect)
            if slider_option.orientation == Qt.Orientation.Vertical:
                groove.adjust(0, self._track_margin, 0, -self._track_margin)
            else:
                groove.adjust(self._track_margin, 0, -self._track_margin, 0)
            return groove

        if sub_control == QStyle.SubControl.SC_ScrollBarSlider:
            slider = QRect(rect)
            target_extent = self.pixelMetric(
                QStyle.PixelMetric.PM_ScrollBarExtent,
                option,
                widget,
            )

            if slider_option.orientation == Qt.Orientation.Vertical:
                slider.setWidth(target_extent)
                slider.moveLeft(rect.center().x() - target_extent // 2)
            else:
                slider.setHeight(target_extent)
                slider.moveTop(rect.center().y() - target_extent // 2)

            return slider

        return rect

class RoundedWindowShell(QWidget):
    """Container that paints an anti-aliased rounded background for the window."""

    def __init__(self, *, radius: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._corner_radius = max(0, radius)
        self._override_color: QColor | None = None

        # ``WA_TranslucentBackground`` prevents Qt from filling the widget with an opaque
        # rectangle before our custom paint routine runs.  The shell therefore relies on the
        # ``paintEvent`` implementation below to draw the rounded surface, ensuring the corners
        # remain transparent when rendered on top of the desktop.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

    def set_corner_radius(self, radius: int) -> None:
        """Update the corner radius and repaint if it changed."""

        clamped = max(0, radius)
        if clamped == self._corner_radius:
            return
        self._corner_radius = clamped
        self.update()

    def corner_radius(self) -> int:
        """Return the current corner radius."""

        return self._corner_radius

    def set_override_color(self, color: QColor | None) -> None:
        """Force the shell to use a specific background colour."""

        if self._override_color == color:
            return
        self._override_color = color
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        """Draw a rounded rectangle using anti-aliased painting."""

        if self.width() <= 0 or self.height() <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setPen(Qt.PenStyle.NoPen)

        effective_color = self._override_color or self.palette().color(QPalette.ColorRole.Window)
        rect = self.rect()
        radius = min(self._corner_radius, min(rect.width(), rect.height()) / 2)

        path = QPainterPath()
        if radius > 0:
            # Offsetting by half a pixel helps keep the curve crisp on high-DPI displays.
            path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        else:
            path.addRect(rect)

        painter.fillPath(path, effective_color)

        # The base class implementation does not paint anything for plain ``QWidget`` instances,
        # but invoking it maintains the usual event chain should Qt's internals change in future.
        super().paintEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)


class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        require_multimedia()

        self.ui = Ui_MainWindow()
        self.ui.setupUi(self, context.library)

        # ``Qt.FramelessWindowHint`` removes the native title bar so the application can provide
        # macOS-style window controls while retaining cross-platform behaviour.  The window flag
        # must be applied after ``setupUi`` so Qt can finish constructing toolbars and the status
        # bar using the default frame.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # Enable per-pixel transparency so the rounded container can blend smoothly with the
        # desktop wallpaper.  The main window itself remains borderless while the dedicated shell
        # widget below paints the actual rounded rectangle chrome.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        # ``_window_corner_radius`` keeps the frameless window visually aligned with native macOS
        # chrome by reintroducing soft corners using a dedicated drawing surface.
        self._window_corner_radius = 12

        # Wrap the autogenerated central widget inside ``RoundedWindowShell`` so we can paint
        # anti-aliased corners without interfering with the structure produced by ``setupUi``.
        original_shell = self.ui.window_shell
        self._rounded_shell: RoundedWindowShell = RoundedWindowShell(
            radius=self._window_corner_radius,
            parent=self,
        )
        # Keep the rounded shell's palette in sync with the window so the anti-aliased
        # backdrop inherits the same colour the application theme expects.
        self._rounded_shell.setPalette(self.palette())
        original_shell.setParent(self._rounded_shell)
        original_shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        original_shell.setAutoFillBackground(False)
        original_shell.setStyleSheet("background-color: transparent;")
        cast(QVBoxLayout, self._rounded_shell.layout()).addWidget(original_shell)
        self.setCentralWidget(self._rounded_shell)

        # ``setupUi`` builds the resize indicator as a free-floating label so we can avoid layout
        # management.  Reparent it now that the rounded shell exists, ensuring the overlay uses
        # the same coordinate space as the frameless window chrome.  Showing the widget here keeps
        # the first paint flicker-free while still letting ``position_resize_widgets`` decide the
        # exact location.
        resize_indicator = getattr(self.ui, "resize_indicator", None)
        if resize_indicator is not None:
            # Reparent the indicator so it shares the rounded shell's coordinate space; this keeps
            # the overlay aligned with the frameless chrome instead of the auto-generated layout.
            resize_indicator.setParent(self._rounded_shell)
            resize_indicator.show()

        self._size_grip = getattr(self.ui, "size_grip", None)
        if self._size_grip is not None:
            # Matching the size grip's parent to the rounded shell ensures Qt routes drag gestures
            # straight to the frameless edge while still letting us position the handle manually.
            self._size_grip.setParent(self._rounded_shell)
            self._size_grip.show()

        # ``FloatingToolTip`` replicates ``QToolTip`` using a styled ``QFrame``
        # so the popup always paints an opaque background.  Sharing a single
        # instance for the window chrome avoids the platform-specific
        # translucency issues that produced unreadable hover hints on Windows
        # when ``WA_TranslucentBackground`` is enabled.  A dedicated
        # application-wide event filter forwards tooltip requests to this
        # helper so every widget inherits the reliable rendering path.
        self._window_tooltip = FloatingToolTip(self)
        self._tooltip_filter: ToolTipEventFilter | None = None
        # Initialise the drag source registry before installing any event filters.
        # ``QApplication.installEventFilter`` begins forwarding events immediately, meaning
        # ``eventFilter`` can run while the constructor is still executing.  Preparing the
        # attribute upfront guarantees the handler always finds a valid container even if Qt
        # dispatches paint or hover events during setup.  The collection is populated with the
        # actual title-bar widgets once they have been constructed a few lines later.
        self._drag_sources: set[QWidget] = set()
        app = QApplication.instance()
        if app is not None:
            # ``ToolTipEventFilter`` keeps hover hints readable on translucent
            # windows by redirecting ``QToolTip`` traffic to the floating helper.
            self._tooltip_filter = ToolTipEventFilter(self._window_tooltip, parent=self)
            app.installEventFilter(self._tooltip_filter)
            app.setProperty("floatingToolTipFilter", self._tooltip_filter)
            # Install the main window as a global event filter so navigation
            # shortcuts can be intercepted regardless of which widget currently
            # owns focus.  This is the only reliable way to override Qt's
            # built-in focus navigation for arrow keys while still allowing the
            # controls to function normally when the gallery view is active.
            app.installEventFilter(self)

        # ``MainController`` owns every piece of non-view logic so the window
        # can focus purely on QWidget behaviour.
        self.controller = MainController(self, context)

        # Position the Live badge after the layout is finalized.
        self.position_live_badge()
        # Place the resize affordances immediately so both the icon and size grip are correct on the
        # first paint instead of waiting for the initial resize event to run.
        self.position_resize_widgets()

        # Keep track of whether the immersive full screen mode is active along with the widgets
        # that were hidden to enter that state so we can restore them exactly as the user left
        # them.  ``_drag_active`` and ``_drag_offset`` are used to emulate the behaviour of a
        # native title bar on frameless windows.
        self._immersive_active = False
        self._hidden_widget_states: list[tuple[QWidget, bool]] = []
        self._splitter_sizes: list[int] | None = None
        self._previous_geometry = self.saveGeometry()
        self._previous_window_state = self.windowState()
        self._drag_active = False
        self._drag_offset = QPoint()
        self._video_controls_enabled_before = self.ui.video_area.controls_enabled()
        self._window_shell_stylesheet = self.ui.window_shell.styleSheet()
        self._player_container_stylesheet = self.ui.player_container.styleSheet()
        self._player_stack_stylesheet = self.ui.player_stack.styleSheet()
        self._immersive_background_applied = False
        self._immersive_visibility_targets: tuple[QWidget, ...]
        self._immersive_visibility_targets = self._build_immersive_targets()
        # ``_qmenu_stylesheet`` caches the rounded popup styling so right-click menus reuse the
        # same colours and radii as the application-controlled drop-down menus.  Individual
        # ``QMenu`` instances receive the stylesheet when they are about to show, avoiding
        # application-wide overrides that could disturb native metrics on unrelated widgets.
        self._qmenu_stylesheet: str = ""
        self._applying_menu_styles = False
        # ``_scrollbar_style`` keeps a single proxy instance alive so every affected scrollbar
        # shares the same Fluent-inspired drawing logic without resorting to global stylesheets.
        self._scrollbar_style: FluentScrollbarStyle | None = None

        # Wire the custom window control buttons to the standard window management actions and
        # connect the immersive viewer exit affordances.
        self.ui.minimize_button.clicked.connect(self.showMinimized)
        self.ui.close_button.clicked.connect(self.close)
        self.ui.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.ui.image_viewer.fullscreenExitRequested.connect(self.exit_fullscreen)
        self.ui.video_area.fullscreenExitRequested.connect(self.exit_fullscreen)

        # Allow dragging from the custom title bar or the window label to emulate the platform
        # window chrome.  The controls themselves keep their default behaviour so users cannot
        # accidentally start moving the window when they meant to click a button.
        self._drag_sources = {self.ui.title_bar, self.ui.window_title_label}

        for source in self._drag_sources:
            source.installEventFilter(self)

        # ``badge_host`` keeps the Live badge aligned with the video viewport;
        # install the filter separately so geometry changes can reposition the
        # overlay without being treated as tooltip traffic.  Although the window
        # now listens at the ``QApplication`` level, explicitly registering here
        # guarantees the geometry updates still arrive even if Qt changes the
        # propagation order in a future release.
        self.ui.badge_host.installEventFilter(self)

        # Allow the window itself to accept focus when the user clicks the chrome so
        # ``keyPressEvent`` can act as a fall-back shortcut path if none of the child
        # widgets have focus at the moment a key is pressed.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self._update_title_bar()
        self._update_fullscreen_button_icon()
        self._apply_menu_styles()
        self._apply_scrollbar_styles()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Tear down background services before the window closes."""

        app = QApplication.instance()
        if app is not None:
            # Remove the global shortcut filter before Qt starts destroying
            # widgets to avoid delivering stray events to partially torn-down
            # objects during shutdown.
            app.removeEventFilter(self)
            if self._tooltip_filter is not None:
                app.removeEventFilter(self._tooltip_filter)
                if app.property("floatingToolTipFilter") == self._tooltip_filter:
                    app.setProperty("floatingToolTipFilter", None)
        self._tooltip_filter = None
        self._window_tooltip.hide_tooltip()

        # ``MainController`` coordinates every component that spawns worker
        # threads (thumbnail rendering, map tile loading, clustering, etc.).
        # Explicitly asking it to shut down here guarantees that all
        # ``QThread``/``QThreadPool`` instances finish before Qt begins
        # destroying widgets, preventing the application process from hanging
        # after the UI is dismissed.
        self.controller.shutdown()
        super().closeEvent(event)

    def statusBar(self) -> ChromeStatusBar:  # type: ignore[override]
        """Return the custom status bar embedded in the rounded shell."""

        return self.ui.status_bar

    def menuBar(self) -> QMenuBar:  # type: ignore[override]
        """Expose the menu bar hosted inside the rounded window shell."""

        return self.ui.menu_bar

    def menu_stylesheet(self) -> str | None:
        """Return the cached ``QMenu`` stylesheet so other widgets can reuse it."""

        return self.get_qmenu_stylesheet()

    def get_qmenu_stylesheet(self) -> str | None:
        """Expose the rounded ``QMenu`` stylesheet, rebuilding it if necessary."""

        if not self._qmenu_stylesheet:
            # If the stylesheet has not been generated yet, trigger a full application so the menu
            # bar and global ``QApplication`` share the same rounded rules before the string is
            # handed to callers.
            if not self._applying_menu_styles:
                self._apply_menu_styles()
            else:
                # Recompute directly when a caller races with ``_apply_menu_styles`` so we can
                # return a valid string without waiting for the guarded method to finish.
                self._qmenu_stylesheet = self._build_menu_styles()[0]
        return self._qmenu_stylesheet or None

    def _build_menu_styles(self) -> tuple[str, str]:
        """Compute palette-aware stylesheets for popup chrome.

        The helper centralises the palette lookups so all menu surfaces reuse the same rounded
        outline and opaque fill even though the main window operates with a translucent
        background.  Returning only the stylesheet strings keeps the menu styling logic focused
        on ``QMenu`` and ``QMenuBar`` while the tooltip palette is managed when the application
        boots.
        """

        palette = self.palette()
        # ``QPalette.ColorRole.Window`` is the shade the rest of the chrome uses for panels such as
        # the sidebar.  Matching that role keeps the menu surfaces visually aligned with the rest of
        # the application shell, whereas ``Base`` resolves to plain white under the bundled
        # palette.
        window_color = self._opaque_color(palette.color(QPalette.ColorRole.Window))
        border_color = self._opaque_color(palette.color(QPalette.ColorRole.Mid))
        text_color = self._opaque_color(palette.color(QPalette.ColorRole.WindowText))
        highlight_color = self._opaque_color(palette.color(QPalette.ColorRole.Highlight))
        highlight_text_color = self._opaque_color(
            palette.color(QPalette.ColorRole.HighlightedText)
        )
        separator_color = self._opaque_color(palette.color(QPalette.ColorRole.Midlight))

        window_color_name = window_color.name()
        border_color_name = border_color.name()
        text_color_name = text_color.name()
        highlight_color_name = highlight_color.name()
        highlight_text_color_name = highlight_text_color.name()
        separator_color_name = separator_color.name()

        # Rounded menus read cleaner against the translucent window shell, so we base both the
        # popup and top-level styles around a shared radius while ensuring menu items retain a
        # subtle inset curve that does not clip their text.
        border_radius_px = 8
        item_radius_px = max(0, border_radius_px - 3)

        # ``QMenu`` widgets no longer draw shadows, so the stylesheet focuses on providing a clean
        # rounded outline that mirrors the rest of the chrome.  The margin and padding values are
        # intentionally small to keep the popup compact while still leaving breathing room for
        # hover highlights.
        qmenu_style = (
            "QMenu {\n"
            f"    background-color: {window_color_name};\n"
            f"    border: 1px solid {border_color_name};\n"
            f"    border-radius: {border_radius_px}px;\n"
            "    padding: 4px;\n"
            "    margin: 0px;\n"
            "}\n"
            "QMenu::item {\n"
            "    background-color: transparent;\n"
            f"    color: {text_color_name};\n"
            "    padding: 5px 20px;\n"
            "    margin: 2px 6px;\n"
            f"    border-radius: {item_radius_px}px;\n"
            "}\n"
            "QMenu::item:selected {\n"
            f"    background-color: {highlight_color_name};\n"
            f"    color: {highlight_text_color_name};\n"
            "}\n"
            "QMenu::separator {\n"
            "    height: 1px;\n"
            f"    background: {separator_color_name};\n"
            "    margin: 4px 10px;\n"
            "}"
        )

        menubar_style = (
            "QMenuBar {\n"
            f"    background-color: {window_color_name};\n"
            "    border-radius: 0px;\n"
            "    padding: 2px;\n"
            "}\n"
            "QMenuBar::item {\n"
            "    background-color: transparent;\n"
            f"    color: {text_color_name};\n"
            "    padding: 4px 10px;\n"
            "    border-radius: 4px;\n"
            "}\n"
            "QMenuBar::item:selected {\n"
            f"    background-color: {highlight_color_name};\n"
            f"    color: {highlight_text_color_name};\n"
            "}\n"
            "QMenuBar::separator {\n"
            f"    background: {separator_color_name};\n"
            "    width: 1px;\n"
            "    margin: 4px 2px;\n"
            "}"
        )

        self._qmenu_stylesheet = qmenu_style
        return qmenu_style, menubar_style

    @staticmethod
    def _opaque_color(color: QColor) -> QColor:
        """Return a colour copy whose alpha channel is forced to full opacity.

        ``WA_TranslucentBackground`` propagates to many popups which in turn causes their
        palettes to report fully transparent colours.  A transparent tone translates to a
        solid black rectangle once the compositor blends it with the desktop.  Forcing every
        shade to have an opaque alpha channel keeps the menu surfaces readable regardless of
        the system theme or platform blending quirks.
        """

        if color.alpha() >= 255:
            return color

        opaque_color = QColor(color)
        opaque_color.setAlpha(255)
        return opaque_color

    def _configure_popup_menu(self, menu: QMenu, stylesheet: str) -> None:
        """Apply frameless styling and rounded menu rules to ``menu``.

        Menu widgets inherit ``WA_TranslucentBackground`` from the frameless window shell so the
        stylesheet-defined rounded corners can blend smoothly with the wallpaper.  Qt disables the
        native window frame in this mode, meaning we must provide the opaque background directly
        through the stylesheet.  The helper ensures that every popup receives the same palette-aware
        rules while also updating the core window flags required for Qt to honour the rounded
        outline.
        """

        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setAutoFillBackground(True)
        menu.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        menu.setWindowFlag(Qt.WindowType.Popup, True)
        menu.setPalette(self.palette())
        menu.setBackgroundRole(QPalette.ColorRole.Base)
        menu.setStyleSheet(stylesheet)
        menu.setGraphicsEffect(None)

    def _install_menu_styling(self, menu: QMenu) -> None:
        """Ensure ``menu`` refreshes its palette-aware styling before it opens.

        ``QMenu`` instances are created lazily by Qt and may persist for the lifetime of the window
        once shown.  Connecting to ``aboutToShow`` allows us to refresh the palette-derived
        stylesheet every time the popup becomes visible, ensuring theme changes or palette updates
        propagate without modifying the application-wide style sheet.  The hook also cascades to
        nested submenus so context popups created by actions inherit the same rounded chrome.
        """

        def apply_style() -> None:
            # Recompute the stylesheet on demand so palette changes are honoured even if no explicit
            # refresh was requested.  ``_build_menu_styles`` updates ``_qmenu_stylesheet`` as a side
            # effect which keeps the helper in sync with other callers that reuse the cached block.
            stylesheet = self._qmenu_stylesheet or self._build_menu_styles()[0]
            self._configure_popup_menu(menu, stylesheet)

            # Recursively attach the hook to child menus.  ``QMenu`` action hierarchies can change
            # dynamically, so performing the walk at show time ensures freshly constructed submenus
            # immediately pick up the frameless styling before their first paint.
            for action in menu.actions():
                child_menu = action.menu()
                if child_menu is not None:
                    self._install_menu_styling(child_menu)

        apply_style()

        if not bool(menu.property(_MENU_STYLE_PROPERTY)):
            menu.aboutToShow.connect(apply_style)
            menu.setProperty(_MENU_STYLE_PROPERTY, True)

    def _apply_scrollbar_styles(self) -> None:
        """Install a proxy style that draws Fluent-inspired scrollbars."""

        base_style = self.style()
        if base_style is None:
            return

        if self._scrollbar_style is not None:
            self._scrollbar_style.deleteLater()
            self._scrollbar_style = None

        self._scrollbar_style = FluentScrollbarStyle(base_style)
        self._scrollbar_style.setParent(self)

        scrollbars: list[QScrollBar] = []
        grid_view = getattr(self.ui, "grid_view", None)
        if grid_view is not None:
            # The gallery grid exposes both scrollbars depending on window width; style both so the
            # Fluent chrome appears regardless of orientation.
            scrollbars.extend([grid_view.verticalScrollBar(), grid_view.horizontalScrollBar()])

        filmstrip = getattr(self.ui, "filmstrip_view", None)
        if filmstrip is not None:
            # The filmstrip only ever reveals the horizontal scrollbar, but keeping the styling
            # explicit prevents Qt from falling back to the generic style when the parent stylesheet
            # is active.
            scrollbars.append(filmstrip.horizontalScrollBar())

        viewer = getattr(self.ui, "image_viewer", None)
        if viewer is not None:
            # The image viewer owns a scroll area internally; walking its children picks up both
            # orientation variants used when the user pans a zoomed image.
            scrollbars.extend(viewer.findChildren(QScrollBar))

        seen: set[int] = set()
        for scrollbar in scrollbars:
            if scrollbar is None:
                continue
            identifier = id(scrollbar)
            if identifier in seen:
                continue
            seen.add(identifier)
            scrollbar.setStyleSheet("")
            scrollbar.setStyle(self._scrollbar_style)
            self._scrollbar_style.polish(scrollbar)
            scrollbar.update()
    def _apply_menu_styles(self) -> None:
        """Force drop-down and context menus to render with opaque backgrounds.

        The application window uses ``WA_TranslucentBackground`` so the rounded chrome can
        blend with the desktop wallpaper.  Without explicit styling, popup menus inherit the
        translucency hint and end up drawing fully transparent surfaces.  Applying a targeted
        stylesheet keeps the menus readable while still respecting the active palette.  The
        stylesheet is installed directly on the menu bar so its drop-downs adopt the opaque rules,
        while the cached block can be reused by ad-hoc ``QMenu`` instances created by child
        widgets (for example, right-click context menus).
        """

        if self._applying_menu_styles:
            return

        self._applying_menu_styles = True
        try:
            qmenu_style, menubar_style = self._build_menu_styles()

            # Apply the stylesheet directly to the menu bar so Qt propagates the palette-aware
            # rules to its drop-down menus.  ``setAutoFillBackground`` and the attribute override
            # ensure the widget paints an opaque surface instead of inheriting the translucent
            # background used by the frameless window chrome.  Updating the palette keeps the menu
            # bar aligned with the rest of the window when themes change.
            self.ui.menu_bar.setStyleSheet(menubar_style)
            self.ui.menu_bar.setAutoFillBackground(True)
            self.ui.menu_bar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.ui.menu_bar.setPalette(self.palette())

            # ``QMenuBar`` owns the drop-down menus presented for each action.  Qt constructs those
            # popups lazily, so iterating the actions here lets us attach the about-to-show hook as
            # soon as each menu exists.  The helper refreshes the palette-aware stylesheet whenever
            # the popup is displayed, keeping the rendering correct without touching the global
            # ``QApplication`` stylesheet.
            for action in self.ui.menu_bar.actions():
                menu = action.menu()
                if menu is None:
                    continue
                self._install_menu_styling(menu)
        finally:
            self._applying_menu_styles = False

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.position_live_badge()
        self.position_resize_widgets()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if watched in self._drag_sources:
            if self._handle_title_bar_drag(event):
                return True

        if watched is self.ui.badge_host and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
        }:
            self.position_live_badge()

        if event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)

            # Always honour Escape presses while immersive mode is active.  Handling
            # it here avoids waiting for the event to bubble to whichever widget
            # currently has focus, providing a consistent exit affordance even when
            # embedded controls consume other navigation keys.
            if key_event.key() == Qt.Key.Key_Escape and self._immersive_active:
                self.exit_fullscreen()
                key_event.accept()
                return True

            # Text-entry widgets should retain native editing behaviour so shortcuts
            # such as arrow-key cursor movement continue to work.  If the active
            # focus widget is a line edit or text editor we bail out early and let
            # Qt deliver the key press normally.
            app = QApplication.instance()
            focus_widget = app.focusWidget() if app is not None else None
            if isinstance(focus_widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
                return super().eventFilter(watched, event)

            if self.ui.view_stack.currentWidget() is self.ui.detail_page:
                if self._handle_detail_view_shortcut(key_event):
                    return True

        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        """Dispatch global keyboard shortcuts that operate on the detail view."""

        if event.key() == Qt.Key.Key_Escape and self._immersive_active:
            self.exit_fullscreen()
            event.accept()
            return

        if self.ui.view_stack.currentWidget() is self.ui.detail_page:
            if self._handle_detail_view_shortcut(event):
                return

        super().keyPressEvent(event)

    def _handle_detail_view_shortcut(self, event: QKeyEvent) -> bool:
        """Handle playback, volume, and navigation shortcuts on the detail page."""

        # Ignore modifier combinations other than the keypad flag so we do not
        # hijack shortcuts that belong to child widgets embedded in the detail
        # page.  The bitwise check lets the handler accept repeats generated by
        # the numeric keypad's arrow keys while rejecting Ctrl/Alt based combos.
        modifiers = event.modifiers()
        disallowed = modifiers & ~Qt.KeyboardModifier.KeypadModifier
        if disallowed:
            return False

        key = event.key()

        state = self.controller.current_player_state()
        is_video_surface = state in {
            PlayerState.PLAYING_VIDEO,
            PlayerState.SHOWING_VIDEO_SURFACE,
        }
        is_live_motion = state == PlayerState.PLAYING_LIVE_MOTION
        is_live_still = state == PlayerState.SHOWING_LIVE_STILL
        can_control_audio = is_video_surface or is_live_motion

        if key == Qt.Key.Key_Space:
            if is_live_still:
                # Replaying a Live Photo while the still frame is on screen
                # keeps the experience consistent with clicking the on-screen
                # replay badge.
                self.controller.replay_live_photo()
                event.accept()
                return True
            if can_control_audio:
                # ``toggle_playback`` transparently handles both pausing active
                # playback and resuming a paused clip, mirroring the player
                # bar's play/pause button.
                self.controller.toggle_playback()
                event.accept()
                return True
            return False

        if key == Qt.Key.Key_M and can_control_audio:
            # Toggling mute here lets the keyboard shortcut share the same
            # state persistence path as the UI controls.
            self.controller.set_media_muted(not self.controller.is_media_muted())
            event.accept()
            return True

        if key in {Qt.Key.Key_Up, Qt.Key.Key_Down} and can_control_audio:
            step = VOLUME_SHORTCUT_STEP if key == Qt.Key.Key_Up else -VOLUME_SHORTCUT_STEP
            current_volume = self.controller.media_volume()
            new_volume = max(0, min(100, current_volume + step))
            if new_volume != current_volume:
                self.controller.set_media_volume(new_volume)
            # Accept the event regardless of whether the volume actually changed so
            # the focus widget does not attempt to interpret the key press as a
            # scroll command when already at the boundary.
            event.accept()
            return True

        if key == Qt.Key.Key_Left:
            self.controller.request_previous_item()
            event.accept()
            return True

        if key == Qt.Key.Key_Right:
            self.controller.request_next_item()
            event.accept()
            return True

        return False

    def changeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        """Refresh the title label whenever Qt updates the window title."""

        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowTitleChange:
            self._update_title_bar()
        elif event.type() == QEvent.Type.PaletteChange:
            # Updating the rounded shell ensures palette transitions repaint the anti-aliased edge
            # without waiting for external resize events.
            self._rounded_shell.setPalette(self.palette())
            self._rounded_shell.update()
            # Regenerate the palette-aware menu stylesheet so newly themed drop-downs immediately
            # adopt the opaque colours without requiring an application restart.
            self._apply_menu_styles()
            self._apply_scrollbar_styles()
        elif event.type() == QEvent.Type.StyleChange:
            # Style changes may arrive when Qt swaps window decorations or the application switches
            # between light and dark modes.  The rounded shell still needs a repaint to keep the
            # anti-aliased frame crisp, but the menu styling will be refreshed on the ensuing
            # palette change (if any) or the next explicit request.
            self._rounded_shell.update()
            self._apply_scrollbar_styles()

    def position_live_badge(self) -> None:
        """Keep the Live badge pinned to the player corner."""

        if self.ui.badge_host is None:
            return
        self.ui.live_badge.move(15, 15)
        self.ui.live_badge.raise_()

    def position_resize_widgets(self) -> None:
        """Pin the resize icon and grip to the shell's lower-right corner."""

        shell = getattr(self, "_rounded_shell", None)
        if shell is None:
            return

        indicator = getattr(self.ui, "resize_indicator", None)
        size_grip = getattr(self, "_size_grip", None)

        margin = 5
        # Determine the footprint required to anchor both widgets.  The maximum of the handle and
        # icon dimensions keeps them perfectly overlapped even if future assets use different sizes.
        width_candidates = [widget.width() for widget in (indicator, size_grip) if widget is not None]
        height_candidates = [widget.height() for widget in (indicator, size_grip) if widget is not None]
        if not width_candidates or not height_candidates:
            return

        target_width = max(width_candidates)
        target_height = max(height_candidates)

        # Offset from the shell's right and bottom edges so the handle always hugs the corner with a
        # consistent margin.  ``max`` prevents the coordinates from becoming negative when the window
        # shrinks below the widget footprint, keeping the affordance visible and reachable.
        target_x = max(0, shell.width() - target_width - margin)
        target_y = max(0, shell.height() - target_height - margin)

        if size_grip is not None:
            size_grip.move(target_x, target_y)
            size_grip.raise_()

        if indicator is not None:
            indicator.move(target_x, target_y)
            indicator.raise_()

    def toggle_fullscreen(self) -> None:
        """Toggle the immersive full screen mode."""

        if self._immersive_active:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self) -> None:
        """Expand the window into an immersive, chrome-free full screen mode."""

        if self._immersive_active:
            return

        resume_after_transition = self.controller.suspend_playback_for_transition()
        ready = self.controller.prepare_fullscreen_asset()
        if not ready:
            # ``prepare_fullscreen_asset`` guarantees the placeholder is visible when no media
            # exists, so the immersive mode can still activate to display a neutral backdrop.
            self.controller.show_placeholder_in_viewer()

        self._previous_geometry = self.saveGeometry()
        self._previous_window_state = self.windowState()
        self._splitter_sizes = self.ui.splitter.sizes()
        with self._suspend_layout_updates():
            self._hidden_widget_states = self._override_visibility(
                self._immersive_visibility_targets,
                visible=False,
            )

            # Store the current playback control state and collapse the overlay.  Keeping the
            # controls enabled allows mouse movement inside the immersive canvas to bring the bar
            # back, matching the expected behaviour of dedicated media players.
            self._video_controls_enabled_before = self.ui.video_area.controls_enabled()
            self.ui.video_area.hide_controls(animate=False)

            # Expanding the splitter after hiding the sidebar ensures the player canvas stretches
            # to occupy the full width.  ``QSplitter`` automatically redistributes the hidden
            # widget's space, so there is no need for manual size adjustments beyond clearing the
            # handle.
            self.ui.splitter.setSizes([0, sum(self._splitter_sizes or [self.width()])])

        self._apply_immersive_backdrop()

        self._immersive_active = True
        self.showFullScreen()
        self._update_fullscreen_button_icon()
        self._schedule_playback_resume(expect_immersive=True, resume=resume_after_transition)

    def exit_fullscreen(self) -> None:
        """Restore the normal window chrome and previously visible widgets."""

        if not self._immersive_active:
            return

        resume_after_transition = self.controller.suspend_playback_for_transition()
        self._immersive_active = False
        self._restore_default_backdrop()
        self.showNormal()

        with self._suspend_layout_updates():
            if self._previous_geometry is not None:
                self.restoreGeometry(self._previous_geometry)
            if self._previous_window_state is not None:
                self.setWindowState(self._previous_window_state)
            if self._splitter_sizes:
                self.ui.splitter.setSizes(self._splitter_sizes)

            for widget, was_visible in self._hidden_widget_states:
                widget.setVisible(was_visible)
            self._hidden_widget_states = []

            self.ui.video_area.set_controls_enabled(self._video_controls_enabled_before)
            if self._video_controls_enabled_before and self.ui.video_area.isVisible():
                self.ui.video_area.show_controls(animate=False)

        self._update_fullscreen_button_icon()
        self._schedule_playback_resume(expect_immersive=False, resume=resume_after_transition)

    # Public API used by sidebar/actions
    def open_album_from_path(self, path: Path) -> None:
        """Expose navigation for legacy callers."""

        self.controller.open_album_from_path(path)

    # Convenience
    def current_selection(self) -> list[Path]:
        """Return absolute paths for every asset selected in the filmstrip."""

        if self.ui.filmstrip_view.selectionModel() is None:
            return []

        indexes = self.ui.filmstrip_view.selectionModel().selectedIndexes()
        return self.controller.paths_from_indexes(indexes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _handle_title_bar_drag(self, event: QEvent) -> bool:
        """Implement mouse dragging for the frameless title bar."""

        if self._immersive_active:
            return False

        if event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = cast(QMouseEvent, event)
            if mouse_event.button() == Qt.MouseButton.LeftButton:
                self._drag_active = True
                self._drag_offset = (
                    mouse_event.globalPosition().toPoint()
                    - self.frameGeometry().topLeft()
                )
                return True
        if event.type() == QEvent.Type.MouseMove and self._drag_active:
            mouse_event = cast(QMouseEvent, event)
            if mouse_event.buttons() & Qt.MouseButton.LeftButton:
                new_pos = mouse_event.globalPosition().toPoint() - self._drag_offset
                self.move(new_pos)
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and self._drag_active:
            self._drag_active = False
            return True
        return False

    def _update_fullscreen_button_icon(self) -> None:
        """Refresh the window control button to match the current mode."""

        if self._immersive_active:
            self.ui.fullscreen_button.setIcon(load_icon("green.restore.circle.svg"))
            self.ui.fullscreen_button.setToolTip("Exit Full Screen")
        else:
            self.ui.fullscreen_button.setIcon(load_icon("green.maximum.circle.svg"))
            self.ui.fullscreen_button.setToolTip("Enter Full Screen")

    def _update_title_bar(self) -> None:
        """Mirror the window title onto the custom title bar label."""

        self.ui.window_title_label.setText(self.windowTitle())

    def _apply_immersive_backdrop(self) -> None:
        """Paint every viewer surface pure black for the immersive presentation."""

        if self._immersive_background_applied:
            return

        # ``QWidget`` stylesheets default to an empty string, so caching the initial values allows
        # the standard theme to be restored precisely instead of falling back to hard coded
        # defaults once the immersive session finishes.
        self._window_shell_stylesheet = self.ui.window_shell.styleSheet()
        self._player_container_stylesheet = self.ui.player_container.styleSheet()
        self._player_stack_stylesheet = self.ui.player_stack.styleSheet()

        self._rounded_shell.set_corner_radius(0)
        self._rounded_shell.set_override_color(QColor("#000000"))
        self.ui.window_shell.setStyleSheet("background-color: #000000;")
        self.ui.player_container.setStyleSheet("background-color: #000000;")
        self.ui.player_stack.setStyleSheet("background-color: #000000;")
        self.ui.image_viewer.set_immersive_background(True)
        self.ui.video_area.set_immersive_background(True)
        self._immersive_background_applied = True

    def _restore_default_backdrop(self) -> None:
        """Revert the temporary black theme applied during immersive mode."""

        if not self._immersive_background_applied:
            return

        self.ui.window_shell.setStyleSheet(self._window_shell_stylesheet)
        self.ui.player_container.setStyleSheet(self._player_container_stylesheet)
        self.ui.player_stack.setStyleSheet(self._player_stack_stylesheet)
        self.ui.image_viewer.set_immersive_background(False)
        self.ui.video_area.set_immersive_background(False)
        self._rounded_shell.set_override_color(None)
        self._rounded_shell.set_corner_radius(self._window_corner_radius)
        self._immersive_background_applied = False

    def _schedule_playback_resume(self, *, expect_immersive: bool, resume: bool) -> None:
        """Resume playback after the window has settled into the target mode."""

        if not resume:
            return

        def _resume() -> None:
            # Skip the resume when the user toggled modes again before the delay elapsed.
            if self._immersive_active != expect_immersive:
                return
            self.controller.resume_playback_after_transition()

        QTimer.singleShot(PLAYBACK_RESUME_DELAY_MS, _resume)

    @contextmanager
    def _suspend_layout_updates(self) -> Iterator[None]:
        """Temporarily disable repaints and splitter signals while chrome toggles run."""

        updates_previously_enabled = self.updatesEnabled()
        splitter_signals_blocked = self.ui.splitter.signalsBlocked()
        self.setUpdatesEnabled(False)
        self.ui.splitter.blockSignals(True)
        try:
            yield
        finally:
            self.ui.splitter.blockSignals(splitter_signals_blocked)
            self.setUpdatesEnabled(updates_previously_enabled)
            if updates_previously_enabled:
                # Trigger a final repaint so the window reflects the batched changes instantly.
                self.update()

    def _override_visibility(
        self, widgets: Iterable[QWidget], *, visible: bool
    ) -> list[tuple[QWidget, bool]]:
        """Apply a shared visibility state and return the previous values for restoration."""

        previous_states: list[tuple[QWidget, bool]] = []
        for widget in widgets:
            previous_states.append((widget, widget.isVisible()))
            widget.setVisible(visible)
        return previous_states

    def _build_immersive_targets(self) -> tuple[QWidget, ...]:
        """Collect every chrome widget that should disappear during immersion."""

        candidates: tuple[QWidget | None, ...] = (
            self.menuBar(),
            self.ui.status_bar,
            self.ui.main_toolbar,
            self.ui.sidebar,
            self.ui.window_chrome,
            self.ui.album_header,
            self.ui.detail_chrome_container,
            self.ui.filmstrip_view,
        )
        return tuple(widget for widget in candidates if widget is not None)

