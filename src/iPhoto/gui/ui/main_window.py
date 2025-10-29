"""Qt widgets composing the main application window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QCloseEvent, QResizeEvent
from PySide6.QtWidgets import QMainWindow, QMenuBar

try:  # pragma: no cover - exercised in packaging scenarios
    from ...appctx import AppContext
except ImportError:  # pragma: no cover - script execution fallback
    from iPhotos.src.iPhoto.appctx import AppContext

from .controllers.main_controller import MainController
from .media import require_multimedia
from .ui_main_window import ChromeStatusBar, Ui_MainWindow
from .window_manager import FramelessWindowManager


class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        require_multimedia()

        self.ui = Ui_MainWindow()
        self.ui.setupUi(self, context.library)

        # ``FramelessWindowManager`` is responsible for every custom chrome
        # behaviour.  The main window therefore remains a thin container that
        # simply forwards lifecycle events to the helper.
        self.window_manager = FramelessWindowManager(self, self.ui)

        # ``MainController`` wires the widgets to the application logic.  The
        # controller reference is forwarded to the window manager so immersive
        # mode can temporarily suspend playback when the window animates.
        self.controller = MainController(self, context)
        self.window_manager.set_controller(self.controller)

        # Retain the behaviour where clicking the chrome gives the window focus
        # so global shortcuts continue to function when no child widget is
        # active.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # ------------------------------------------------------------------
    # QWidget overrides
    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Tear down background services before the window closes."""

        self.window_manager.cleanup()
        self.controller.shutdown()
        super().closeEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.window_manager.handle_resize_event(event)

    def changeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        super().changeEvent(event)
        self.window_manager.handle_change_event(event)

    # ------------------------------------------------------------------
    # Window chrome accessors used by child widgets
    def statusBar(self) -> ChromeStatusBar:  # type: ignore[override]
        """Return the custom status bar embedded in the rounded shell."""

        return self.ui.status_bar

    def menuBar(self) -> QMenuBar:  # type: ignore[override]
        """Expose the menu bar hosted inside the rounded window shell."""

        return self.window_manager.menuBar()

    def menu_stylesheet(self) -> str | None:
        """Return the cached ``QMenu`` stylesheet so other widgets can reuse it."""

        return self.window_manager.menu_stylesheet()

    def get_qmenu_stylesheet(self) -> str | None:
        """Expose the rounded ``QMenu`` stylesheet, rebuilding it if necessary."""

        return self.window_manager.get_qmenu_stylesheet()

    # ------------------------------------------------------------------
    # Convenience wrappers kept for backwards compatibility
    def position_live_badge(self) -> None:
        """Allow legacy callers to reposition the Live badge."""

        self.window_manager.position_live_badge()

    def position_resize_widgets(self) -> None:
        """Allow legacy callers to reposition the resize affordances."""

        self.window_manager.position_resize_widgets()

    def toggle_fullscreen(self) -> None:
        """Toggle the immersive full screen mode."""

        self.window_manager.toggle_fullscreen()

    def enter_fullscreen(self) -> None:
        """Expand the window into the immersive presentation mode."""

        self.window_manager.enter_fullscreen()

    def exit_fullscreen(self) -> None:
        """Restore the standard chrome from immersive mode."""

        self.window_manager.exit_fullscreen()

    # ------------------------------------------------------------------
    # Public API used by sidebar/actions
    def open_album_from_path(self, path: Path) -> None:
        """Expose navigation for legacy callers."""

        self.controller.open_album_from_path(path)

    def current_selection(self) -> list[Path]:
        """Return absolute paths for every asset selected in the filmstrip."""

        if self.ui.filmstrip_view.selectionModel() is None:
            return []

        indexes = self.ui.filmstrip_view.selectionModel().selectedIndexes()
        return self.controller.paths_from_indexes(indexes)

