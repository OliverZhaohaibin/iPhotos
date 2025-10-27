"""Qt widgets composing the main application window."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, cast

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenu,
    QMenuBar,
    QVBoxLayout,
    QWidget,
)

# ``main_window`` can be imported either via ``iPhoto.gui`` (package execution)
# or ``iPhotos.src.iPhoto.gui`` (legacy test harness).  The absolute import
# keeps script-mode launches working where the relative form lacks package
# context.
try:  # pragma: no cover - exercised in packaging scenarios
    from ...appctx import AppContext
except ImportError:  # pragma: no cover - script execution fallback
    from iPhotos.src.iPhoto.appctx import AppContext

from .controllers.main_controller import MainController
from .media import require_multimedia
from .styles import build_global_stylesheet, build_menu_styles, ensure_opaque_color
from .ui_main_window import ChromeStatusBar, Ui_MainWindow
from .icons import load_icon
from .widgets.custom_tooltip import FloatingToolTip, ToolTipEventFilter


# Small delay that gives Qt time to settle window transitions before resuming playback.
PLAYBACK_RESUME_DELAY_MS = 120


class WindowResizeGrip(QWidget):
    """Interactive handle that resizes the frameless window when dragged."""

    def __init__(self, window: QMainWindow, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._dragging = False
        self._press_pos = QPoint()
        self._start_geometry = QRect()

        # A compact square mirrors modern grip affordances while still offering a comfortable
        # hit target.  The diagonal cursor communicates the behaviour without any extra chrome.
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setToolTip("Resize window")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        """Capture the initial geometry so drag deltas can be applied precisely."""

        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return

        if self._window.isMaximized() or self._window.isFullScreen():
            # Qt ignores resize requests while the window is maximised or in immersive mode, so we
            # abort the drag session immediately to avoid misleading the user.
            event.ignore()
            return

        self._dragging = True
        self._press_pos = event.globalPosition().toPoint()
        self._start_geometry = self._window.geometry()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        """Resize the window by applying the cursor delta to the stored geometry."""

        if not self._dragging:
            event.ignore()
            return

        if not (event.buttons() & Qt.MouseButton.LeftButton):
            event.ignore()
            return

        delta = event.globalPosition().toPoint() - self._press_pos
        new_width = self._start_geometry.width() + delta.x()
        new_height = self._start_geometry.height() + delta.y()

        # Clamp the requested geometry to honour the window's sizing constraints.
        new_width = max(self._window.minimumWidth(), new_width)
        new_height = max(self._window.minimumHeight(), new_height)

        max_width = self._window.maximumWidth()
        if max_width > 0:
            new_width = min(max_width, new_width)

        max_height = self._window.maximumHeight()
        if max_height > 0:
            new_height = min(max_height, new_height)

        self._window.resize(new_width, new_height)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        """Finish the drag interaction when the cursor button is released."""

        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            event.accept()
        else:
            event.ignore()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        """Render a subtle triangular grip that honours the active palette."""

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        palette = self._window.palette()
        shadow_colour = ensure_opaque_color(palette.color(QPalette.ColorRole.Mid))
        highlight_colour = ensure_opaque_color(palette.color(QPalette.ColorRole.Light))

        triangle = QPainterPath()
        triangle.moveTo(self.width(), 0)
        triangle.lineTo(self.width(), self.height())
        triangle.lineTo(0, self.height())
        triangle.closeSubpath()
        painter.fillPath(triangle, shadow_colour)

        inset = QPainterPath()
        inset.moveTo(self.width() - 4, 2)
        inset.lineTo(self.width() - 2, self.height() - 4)
        inset.lineTo(2, self.height() - 2)
        inset.closeSubpath()
        painter.fillPath(inset, highlight_colour)

        super().paintEvent(event)

    def reposition(self) -> None:
        """Anchor the grip to the parent's bottom-right corner."""

        parent = self.parentWidget()
        if parent is None:
            return

        margin = 6
        x_pos = max(0, parent.width() - self.width() - margin)
        y_pos = max(0, parent.height() - self.height() - margin)
        self.move(x_pos, y_pos)
        self.raise_()


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


class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        require_multimedia()

        # Initialise the rounded shell placeholder before Qt begins dispatching events during
        # ``setupUi``.  ``changeEvent`` can fire while widgets are being constructed which means
        # attribute access must succeed even before the final shell is created.
        self._rounded_shell: RoundedWindowShell | None = None

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
        self._rounded_shell = RoundedWindowShell(
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

        self._resize_grip = WindowResizeGrip(self, parent=self._rounded_shell)
        self._resize_grip.reposition()

        # ``FloatingToolTip`` replicates ``QToolTip`` using a styled ``QFrame``
        # so the popup always paints an opaque background.  Sharing a single
        # instance for the window chrome avoids the platform-specific
        # translucency issues that produced unreadable hover hints on Windows
        # when ``WA_TranslucentBackground`` is enabled.  A dedicated
        # application-wide event filter forwards tooltip requests to this
        # helper so every widget inherits the reliable rendering path.
        self._window_tooltip = FloatingToolTip(self)
        self._tooltip_filter: ToolTipEventFilter | None = None
        app = QApplication.instance()
        if app is not None:
            self._tooltip_filter = ToolTipEventFilter(self._window_tooltip, parent=self)
            app.installEventFilter(self._tooltip_filter)
            app.setProperty("floatingToolTipFilter", self._tooltip_filter)

        # ``MainController`` owns every piece of non-view logic so the window
        # can focus purely on QWidget behaviour.
        self.controller = MainController(self, context)

        # Position the Live badge after the layout is finalized.
        self.position_live_badge()

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
        # same colours and radii as the application-controlled drop-down menus.  The
        # ``_global_menu_stylesheet`` marker tracks which rules were last injected into the
        # ``QApplication`` instance, letting us replace them cleanly when the palette changes
        # without building up duplicate blocks.
        self._qmenu_stylesheet: str = ""
        self._applying_menu_styles = False
        self._current_global_stylesheet = ""

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
        # overlay without being treated as tooltip traffic.
        self.ui.badge_host.installEventFilter(self)

        self._update_title_bar()
        self._update_fullscreen_button_icon()
        self._apply_menu_styles()
        app = QApplication.instance()
        if app is not None:
            self._current_global_stylesheet = app.styleSheet()
            if not self._current_global_stylesheet:
                self.refresh_global_stylesheet()
        self._update_resize_grip_visibility()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Tear down background services before the window closes."""

        if self._tooltip_filter is not None:
            app = QApplication.instance()
            if app is not None:
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
                self._qmenu_stylesheet = build_menu_styles(self.palette())[0]
        return self._qmenu_stylesheet or None

    def refresh_global_stylesheet(self) -> None:
        """Rebuild and apply the combined application stylesheet in one pass."""

        app = QApplication.instance()
        if app is None:
            return

        combined_stylesheet = build_global_stylesheet(app.palette())
        if self._current_global_stylesheet == combined_stylesheet:
            return

        app.setStyleSheet(combined_stylesheet)
        self._current_global_stylesheet = combined_stylesheet

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
            qmenu_style, menubar_style = build_menu_styles(self.palette())
            self._qmenu_stylesheet = qmenu_style

            # Apply the stylesheet directly to the menu bar so Qt propagates the palette-aware
            # rules to its drop-down menus.  ``setAutoFillBackground`` and the attribute override
            # ensure the widget paints an opaque surface instead of inheriting the translucent
            # background used by the frameless window chrome.
            self.ui.menu_bar.setStyleSheet(menubar_style)
            self.ui.menu_bar.setAutoFillBackground(True)
            self.ui.menu_bar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

            # ``QMenuBar`` owns the drop-down menus presented for each action.  Qt constructs those
            # popups lazily, so iterating the actions here lets us retrofit the required window
            # flags once they exist.  ``FramelessWindowHint`` allows the stylesheet to control the
            # rounded outline while ``WA_TranslucentBackground`` keeps the corners transparent so
            # the painted background remains visible.  Applying the cached stylesheet directly
            # ensures the popups match the context menus that reuse the same helper.
            for action in self.ui.menu_bar.actions():
                menu = action.menu()
                if menu is None:
                    continue
                self._configure_popup_menu(menu, qmenu_style)
        finally:
            self._applying_menu_styles = False

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.position_live_badge()
        if self._resize_grip.isVisible():
            self._resize_grip.reposition()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched in self._drag_sources:
            if self._handle_title_bar_drag(event):
                return True

        if watched is self.ui.badge_host and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
        }:
            self.position_live_badge()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        """Allow pressing Escape to exit immersive full screen mode."""

        if event.key() == Qt.Key.Key_Escape and self._immersive_active:
            self.exit_fullscreen()
            event.accept()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        """Refresh the title label whenever Qt updates the window title."""

        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowTitleChange:
            self._update_title_bar()
        elif event.type() == QEvent.Type.PaletteChange:
            # Updating the rounded shell ensures palette transitions repaint the anti-aliased edge
            # without waiting for external resize events.
            if self._rounded_shell is not None:
                self._rounded_shell.update()
            # Regenerate the palette-aware menu stylesheet so newly themed drop-downs immediately
            # adopt the opaque colours without requiring an application restart.
            self._apply_menu_styles()
            self.refresh_global_stylesheet()
        elif event.type() == QEvent.Type.StyleChange:
            # Style changes may arrive when Qt swaps window decorations or the application switches
            # between light and dark modes.  The rounded shell still needs a repaint to keep the
            # anti-aliased frame crisp, but the menu styling will be refreshed on the ensuing
            # palette change (if any) or the next explicit request.
            if self._rounded_shell is not None:
                self._rounded_shell.update()
        elif event.type() == QEvent.Type.WindowStateChange:
            self._update_resize_grip_visibility()

    def position_live_badge(self) -> None:
        """Keep the Live badge pinned to the player corner."""

        if self.ui.badge_host is None:
            return
        self.ui.live_badge.move(15, 15)
        self.ui.live_badge.raise_()

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
        self._update_resize_grip_visibility()
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
        self._update_resize_grip_visibility()
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

    def _update_resize_grip_visibility(self) -> None:
        """Show the resize grip only when the window accepts manual resizing."""

        should_show = (
            not self._immersive_active
            and not self.isFullScreen()
            and not self.isMaximized()
        )
        self._resize_grip.setVisible(should_show)
        if should_show:
            self._resize_grip.reposition()

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

