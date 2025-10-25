"""Qt widgets composing the main application window."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, cast

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeyEvent, QMouseEvent, QPainterPath, QRegion
from PySide6.QtWidgets import QMainWindow, QWidget

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
from .ui_main_window import Ui_MainWindow
from .icons import load_icon


# Small delay that gives Qt time to settle window transitions before resuming playback.
PLAYBACK_RESUME_DELAY_MS = 120


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
        # ``_window_corner_radius`` keeps the frameless window visually aligned with native macOS
        # chrome by reintroducing soft corners via a mask.  The value matches the platform default
        # while remaining visually appealing on other operating systems.
        self._window_corner_radius = 12

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

        self._update_title_bar()
        self._update_fullscreen_button_icon()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Tear down background services before the window closes."""

        # ``MainController`` coordinates every component that spawns worker
        # threads (thumbnail rendering, map tile loading, clustering, etc.).
        # Explicitly asking it to shut down here guarantees that all
        # ``QThread``/``QThreadPool`` instances finish before Qt begins
        # destroying widgets, preventing the application process from hanging
        # after the UI is dismissed.
        self.controller.shutdown()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.position_live_badge()
        self._apply_window_mask()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._apply_window_mask()

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
        self.clearMask()
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
        self._apply_window_mask()

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
                self._drag_offset = mouse_event.globalPosition().toPoint() - self.frameGeometry().topLeft()
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
            self.statusBar(),
            self.ui.main_toolbar,
            self.ui.sidebar,
            self.ui.window_chrome,
            self.ui.album_header,
            self.ui.detail_chrome_container,
            self.ui.filmstrip_view,
        )
        return tuple(widget for widget in candidates if widget is not None)

    def _apply_window_mask(self) -> None:
        """Apply a rounded rectangle mask so the frameless window keeps soft corners."""

        if not self.isVisible():
            return

        # The mask must be cleared while the window is in immersive full screen mode to avoid
        # cropping the screen contents.  Once the standard chrome returns, the rounded shape can be
        # restored during the next resize or show event.
        if self._immersive_active or self.isFullScreen():
            self.clearMask()
            return

        rect = self.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return

        # ``QPainterPath`` produces an anti-aliased outline that converts cleanly into a region.
        # This keeps the corners smooth on high-DPI displays while avoiding the performance costs of
        # using a translucent top-level window on platforms that do not support GPU compositing.
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), self._window_corner_radius, self._window_corner_radius)
        region = QRegion(path.toFillPolygon().toPolygon())
        self.setMask(region)
