"""Helpers responsible for the frameless main window chrome."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Iterator, TYPE_CHECKING, cast

from PySide6.QtCore import Property, QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import (
    QColor,
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

from .icon import load_icon
from .ui_main_window import Ui_MainWindow
from .widgets.custom_tooltip import FloatingToolTip, ToolTipEventFilter

if TYPE_CHECKING:  # pragma: no cover - used only for type checking
    from PySide6.QtGui import QResizeEvent

    from .controllers.main_controller import MainController


# ``PLAYBACK_RESUME_DELAY_MS`` mirrors the behaviour found in the original
# ``MainWindow`` implementation.  The small pause gives Qt time to settle the
# window transition (for example switching into or out of full screen) before
# multimedia playback resumes.  Skipping the delay causes videos to stutter
# visibly on macOS and Windows when the compositor is still applying the size
# changes.
PLAYBACK_RESUME_DELAY_MS = 120


class RoundedWindowShell(QWidget):
    """Container that draws the translucent rounded chrome for the window."""

    def __init__(self, *, radius: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._corner_radius = max(0, radius)
        self._override_color: QColor | None = None

        # ``WA_TranslucentBackground`` prevents Qt from filling the widget with
        # an opaque rectangle before our custom paint routine executes.  The
        # shell therefore relies on ``paintEvent`` to render the rounded surface
        # while the actual ``QMainWindow`` remains frameless and fully
        # transparent.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

    # ------------------------------------------------------------------
    # Public API used by ``FramelessWindowManager``
    def _get_override_color(self) -> QColor:
        """Return the colour currently used when painting the rounded shell."""

        # ``QPropertyAnimation`` requires a getter when driving a ``Property``
        # on PySide.  Returning the active override keeps the animation in sync
        # with whatever value :meth:`set_override_color` most recently applied.
        return self._override_color or self.palette().color(QPalette.ColorRole.Window)

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

    # Expose a Qt property so controllers can animate the background colour
    # without reaching into private attributes.  The setter already triggers a
    # repaint, so the animation simply drives the property and the shell reacts.
    overrideColor = Property(QColor, _get_override_color, set_override_color)

    # ------------------------------------------------------------------
    # QWidget overrides
    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        """Draw an anti-aliased rounded rectangle matching the window palette."""

        if self.width() <= 0 or self.height() <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setPen(Qt.PenStyle.NoPen)

        effective_color = self._override_color or self.palette().color(
            QPalette.ColorRole.Window
        )
        rect = self.rect()
        radius = min(self._corner_radius, min(rect.width(), rect.height()) / 2)

        path = QPainterPath()
        if radius > 0:
            # Offsetting by half a pixel keeps the curve crisp on high-DPI
            # displays.
            path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        else:
            path.addRect(rect)

        painter.fillPath(path, effective_color)
        super().paintEvent(event)


class FramelessWindowManager(QObject):
    """Encapsulate the custom chrome applied to the main application window."""

    def __init__(self, window: QMainWindow, ui: Ui_MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self._ui = ui
        self._controller: MainController | None = None

        # Frameless setup -------------------------------------------------
        self._window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._window.setAutoFillBackground(False)

        self._window_corner_radius = 12
        self._rounded_shell = self._create_shell()
        self._size_grip = getattr(self._ui, "size_grip", None)
        self._window_tooltip = FloatingToolTip(self._window)
        self._tooltip_filter: ToolTipEventFilter | None = None
        self._drag_sources: set[QWidget] = set()

        self._immersive_active = False
        self._hidden_widget_states: list[tuple[QWidget, bool]] = []
        self._splitter_sizes: list[int] | None = None
        self._previous_geometry = self._window.saveGeometry()
        self._previous_window_state = self._window.windowState()
        self._drag_active = False
        self._drag_offset = QPoint()
        self._video_controls_enabled_before = self._ui.video_area.controls_enabled()
        self._window_shell_stylesheet = self._ui.window_shell.styleSheet()
        self._player_container_stylesheet = self._ui.player_container.styleSheet()
        self._player_stack_stylesheet = self._ui.player_stack.styleSheet()
        self._immersive_background_applied = False
        self._immersive_visibility_targets = self._build_immersive_targets()

        self._qmenu_stylesheet: str = ""
        self._global_menu_stylesheet: str | None = None
        self._applying_menu_styles = False

        self._install_tooltip_filter()
        self._configure_window_controls()
        self._configure_drag_sources()
        self._apply_menu_styles()
        self.position_live_badge()
        self.position_resize_widgets()

    # ------------------------------------------------------------------
    # Lifecycle helpers
    def set_controller(self, controller: MainController) -> None:
        """Provide the ``MainController`` reference required for immersive mode."""

        self._controller = controller

    def cleanup(self) -> None:
        """Remove global filters and hide tooltip widgets during shutdown."""

        app = QApplication.instance()
        if app is not None:
            if self._tooltip_filter is not None:
                app.removeEventFilter(self._tooltip_filter)
                if app.property("floatingToolTipFilter") == self._tooltip_filter:
                    app.setProperty("floatingToolTipFilter", None)
        self._tooltip_filter = None
        self._window_tooltip.hide_tooltip()

    # ------------------------------------------------------------------
    # Menu helpers
    def menuBar(self) -> QMenuBar:
        """Expose the menu bar hosted inside the rounded shell."""

        return self._ui.menu_bar

    def menu_stylesheet(self) -> str | None:
        """Return the cached ``QMenu`` stylesheet so other widgets can reuse it."""

        return self.get_qmenu_stylesheet()

    def get_qmenu_stylesheet(self) -> str | None:
        """Expose the rounded ``QMenu`` stylesheet, rebuilding it if necessary."""

        if not self._qmenu_stylesheet:
            if not self._applying_menu_styles:
                self._apply_menu_styles()
            else:
                self._qmenu_stylesheet = self._build_menu_styles()[0]
        return self._qmenu_stylesheet or None

    # ------------------------------------------------------------------
    # Event forwarding helpers used by ``MainWindow``
    def handle_resize_event(self, event: QResizeEvent) -> None:
        """Reposition overlays whenever the window geometry changes."""

        _ = event  # ``QResizeEvent`` is unused but kept for signature clarity.
        self.position_live_badge()
        self.position_resize_widgets()

    def handle_change_event(self, event: QEvent) -> None:
        """Update palette-dependent chrome when Qt notifies about state changes."""

        if event.type() == QEvent.Type.WindowTitleChange:
            self._update_title_bar()
        elif event.type() == QEvent.Type.PaletteChange:
            self._rounded_shell.update()
            self._apply_menu_styles()

    # ------------------------------------------------------------------
    # Window chrome helpers
    def position_live_badge(self) -> None:
        """Keep the Live badge pinned to the player corner."""

        if self._ui.badge_host is None:
            return
        self._ui.live_badge.move(15, 15)
        self._ui.live_badge.raise_()

    def position_resize_widgets(self) -> None:
        """Pin the resize icon and grip to the shell's lower-right corner."""

        shell = getattr(self, "_rounded_shell", None)
        if shell is None:
            return

        indicator = getattr(self._ui, "resize_indicator", None)
        size_grip = getattr(self, "_size_grip", None)

        margin = 5
        width_candidates = [
            widget.width() for widget in (indicator, size_grip) if widget is not None
        ]
        height_candidates = [
            widget.height() for widget in (indicator, size_grip) if widget is not None
        ]
        if not width_candidates or not height_candidates:
            return

        target_width = max(width_candidates)
        target_height = max(height_candidates)
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
        if self._controller is None:
            return

        resume_after_transition = self._controller.suspend_playback_for_transition()
        ready = self._controller.prepare_fullscreen_asset()
        if not ready:
            self._controller.show_placeholder_in_viewer()

        self._previous_geometry = self._window.saveGeometry()
        self._previous_window_state = self._window.windowState()
        self._splitter_sizes = self._ui.splitter.sizes()
        with self._suspend_layout_updates():
            self._hidden_widget_states = self._override_visibility(
                self._immersive_visibility_targets, visible=False
            )

            self._video_controls_enabled_before = (
                self._ui.video_area.controls_enabled()
            )
            self._ui.video_area.hide_controls(animate=False)
            self._ui.splitter.setSizes([0, sum(self._splitter_sizes or [self._window.width()])])

        self._apply_immersive_backdrop()

        self._immersive_active = True
        self._window.showFullScreen()
        self._update_fullscreen_button_icon()
        self._schedule_playback_resume(
            expect_immersive=True, resume=resume_after_transition
        )

    def exit_fullscreen(self) -> None:
        """Restore the normal window chrome and previously visible widgets."""

        if not self._immersive_active:
            return
        if self._controller is None:
            return

        resume_after_transition = self._controller.suspend_playback_for_transition()
        self._immersive_active = False
        self._restore_default_backdrop()
        self._window.showNormal()

        with self._suspend_layout_updates():
            if self._previous_geometry is not None:
                self._window.restoreGeometry(self._previous_geometry)
            if self._previous_window_state is not None:
                self._window.setWindowState(self._previous_window_state)
            if self._splitter_sizes:
                self._ui.splitter.setSizes(self._splitter_sizes)

            for widget, was_visible in self._hidden_widget_states:
                widget.setVisible(was_visible)
            self._hidden_widget_states = []

            self._ui.video_area.set_controls_enabled(
                self._video_controls_enabled_before
            )
            if (
                self._video_controls_enabled_before
                and self._ui.video_area.isVisible()
            ):
                self._ui.video_area.show_controls(animate=False)

        self._update_fullscreen_button_icon()
        self._schedule_playback_resume(
            expect_immersive=False, resume=resume_after_transition
        )

    def is_immersive_active(self) -> bool:
        """Return ``True`` when the window is in immersive full screen mode."""

        return self._immersive_active

    # ------------------------------------------------------------------
    # QObject overrides
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        """Handle title-bar dragging and badge positioning."""

        if watched in self._drag_sources:
            if self._handle_title_bar_drag(event):
                return True

        if watched is self._ui.badge_host and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
        }:
            self.position_live_badge()

        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Internal helpers
    def _create_shell(self) -> RoundedWindowShell:
        original_shell = self._ui.window_shell
        rounded_shell = RoundedWindowShell(radius=self._window_corner_radius, parent=self._window)
        rounded_shell.setPalette(self._window.palette())
        original_shell.setParent(rounded_shell)
        original_shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        original_shell.setAutoFillBackground(False)
        original_shell.setStyleSheet("background-color: transparent;")
        cast(QVBoxLayout, rounded_shell.layout()).addWidget(original_shell)
        self._window.setCentralWidget(rounded_shell)

        resize_indicator = getattr(self._ui, "resize_indicator", None)
        if resize_indicator is not None:
            resize_indicator.setParent(rounded_shell)
            resize_indicator.show()

        size_grip = getattr(self._ui, "size_grip", None)
        if size_grip is not None:
            size_grip.setParent(rounded_shell)
            size_grip.show()
        return rounded_shell

    def _install_tooltip_filter(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        self._tooltip_filter = ToolTipEventFilter(self._window_tooltip, parent=self)
        app.installEventFilter(self._tooltip_filter)
        app.setProperty("floatingToolTipFilter", self._tooltip_filter)

    def _configure_window_controls(self) -> None:
        self._ui.minimize_button.clicked.connect(self._window.showMinimized)
        self._ui.close_button.clicked.connect(self._window.close)
        self._ui.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self._ui.image_viewer.fullscreenExitRequested.connect(self.exit_fullscreen)
        self._ui.video_area.fullscreenExitRequested.connect(self.exit_fullscreen)

    def _configure_drag_sources(self) -> None:
        self._drag_sources = {self._ui.title_bar, self._ui.window_title_label}
        for source in self._drag_sources:
            source.installEventFilter(self)

        self._ui.badge_host.installEventFilter(self)

    def _handle_title_bar_drag(self, event: QEvent) -> bool:
        if self._immersive_active:
            return False

        if event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = cast(QMouseEvent, event)
            if mouse_event.button() == Qt.MouseButton.LeftButton:
                self._drag_active = True
                self._drag_offset = (
                    mouse_event.globalPosition().toPoint()
                    - self._window.frameGeometry().topLeft()
                )
                return True
        if event.type() == QEvent.Type.MouseMove and self._drag_active:
            mouse_event = cast(QMouseEvent, event)
            if mouse_event.buttons() & Qt.MouseButton.LeftButton:
                new_pos = mouse_event.globalPosition().toPoint() - self._drag_offset
                self._window.move(new_pos)
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and self._drag_active:
            self._drag_active = False
            return True
        return False

    def _update_fullscreen_button_icon(self) -> None:
        if self._immersive_active:
            self._ui.fullscreen_button.setIcon(load_icon("green.restore.circle.svg"))
            self._ui.fullscreen_button.setToolTip("Exit Full Screen")
        else:
            self._ui.fullscreen_button.setIcon(load_icon("green.maximum.circle.svg"))
            self._ui.fullscreen_button.setToolTip("Enter Full Screen")

    def _update_title_bar(self) -> None:
        self._ui.window_title_label.setText(self._window.windowTitle())

    def _apply_immersive_backdrop(self) -> None:
        if self._immersive_background_applied:
            return

        self._window_shell_stylesheet = self._ui.window_shell.styleSheet()
        self._player_container_stylesheet = self._ui.player_container.styleSheet()
        self._player_stack_stylesheet = self._ui.player_stack.styleSheet()

        self._rounded_shell.set_corner_radius(0)
        self._rounded_shell.set_override_color(QColor("#000000"))
        self._ui.window_shell.setStyleSheet("background-color: #000000;")
        self._ui.player_container.setStyleSheet("background-color: #000000;")
        self._ui.player_stack.setStyleSheet("background-color: #000000;")
        self._ui.image_viewer.set_immersive_background(True)
        self._ui.video_area.set_immersive_background(True)
        self._immersive_background_applied = True

    def _restore_default_backdrop(self) -> None:
        if not self._immersive_background_applied:
            return

        self._ui.window_shell.setStyleSheet(self._window_shell_stylesheet)
        self._ui.player_container.setStyleSheet(self._player_container_stylesheet)
        self._ui.player_stack.setStyleSheet(self._player_stack_stylesheet)
        self._ui.image_viewer.set_immersive_background(False)
        self._ui.video_area.set_immersive_background(False)
        self._rounded_shell.set_override_color(None)
        self._rounded_shell.set_corner_radius(self._window_corner_radius)
        self._immersive_background_applied = False

    def _schedule_playback_resume(self, *, expect_immersive: bool, resume: bool) -> None:
        if not resume:
            return
        if self._controller is None:
            return

        def _resume() -> None:
            if self._immersive_active != expect_immersive:
                return
            self._controller.resume_playback_after_transition()

        QTimer.singleShot(PLAYBACK_RESUME_DELAY_MS, _resume)

    @contextmanager
    def _suspend_layout_updates(self) -> Iterator[None]:
        updates_previously_enabled = self._window.updatesEnabled()
        splitter_signals_blocked = self._ui.splitter.signalsBlocked()
        self._window.setUpdatesEnabled(False)
        self._ui.splitter.blockSignals(True)
        try:
            yield
        finally:
            self._ui.splitter.blockSignals(splitter_signals_blocked)
            self._window.setUpdatesEnabled(updates_previously_enabled)
            if updates_previously_enabled:
                self._window.update()

    def _override_visibility(
        self, widgets: Iterable[QWidget], *, visible: bool
    ) -> list[tuple[QWidget, bool]]:
        previous_states: list[tuple[QWidget, bool]] = []
        for widget in widgets:
            previous_states.append((widget, widget.isVisible()))
            widget.setVisible(visible)
        return previous_states

    def _build_immersive_targets(self) -> tuple[QWidget, ...]:
        candidates: tuple[QWidget | None, ...] = (
            self.menuBar(),
            self._ui.menu_bar_container,
            self._ui.status_bar,
            self._ui.sidebar,
            self._ui.window_chrome,
            self._ui.detail_chrome_container,
            self._ui.filmstrip_view,
        )
        return tuple(widget for widget in candidates if widget is not None)

    def _build_menu_styles(self) -> tuple[str, str]:
        palette = self._window.palette()
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

        border_radius_px = 8
        item_radius_px = max(0, border_radius_px - 3)

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
        if color.alpha() >= 255:
            return color
        opaque_color = QColor(color)
        opaque_color.setAlpha(255)
        return opaque_color

    def _configure_popup_menu(self, menu: QMenu, stylesheet: str) -> None:
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setAutoFillBackground(True)
        menu.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        menu.setWindowFlag(Qt.WindowType.Popup, True)
        menu.setPalette(self._window.palette())
        menu.setBackgroundRole(QPalette.ColorRole.Base)
        menu.setStyleSheet(stylesheet)
        menu.setGraphicsEffect(None)

    def _apply_menu_styles(self) -> None:
        if self._applying_menu_styles:
            return

        self._applying_menu_styles = True
        try:
            qmenu_style, menubar_style = self._build_menu_styles()
            self._ui.menu_bar.setStyleSheet(menubar_style)
            self._ui.menu_bar.setAutoFillBackground(True)
            self._ui.menu_bar.setAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground, False
            )

            app = QApplication.instance()
            if app is not None:
                existing = app.styleSheet()
                if self._global_menu_stylesheet and self._global_menu_stylesheet in existing:
                    existing = existing.replace(self._global_menu_stylesheet, "").strip()

                combined_parts = [part for part in (existing, qmenu_style) if part]
                app.setStyleSheet("\n".join(combined_parts))
                self._global_menu_stylesheet = qmenu_style
            else:
                self._global_menu_stylesheet = qmenu_style

            for action in self._ui.menu_bar.actions():
                menu = action.menu()
                if menu is None:
                    continue
                self._configure_popup_menu(menu, qmenu_style)
        finally:
            self._applying_menu_styles = False

