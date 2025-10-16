"""Qt widgets composing the main application window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow

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


class MainWindow(QMainWindow):
    """Primary window for the desktop experience."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        require_multimedia()

        self.ui = Ui_MainWindow()
        self.ui.setupUi(self, context.library)

        # ``MainController`` owns every piece of non-view logic so the window
        # can focus purely on QWidget behaviour.
        self.controller = MainController(self, context)

        # Position the Live badge after the layout is finalized.
        self.position_live_badge()

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

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched is self.ui.badge_host and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
        }:
            self.position_live_badge()
        return super().eventFilter(watched, event)

    def position_live_badge(self) -> None:
        """Keep the Live badge pinned to the player corner."""

        if self.ui.badge_host is None:
            return
        self.ui.live_badge.move(15, 15)
        self.ui.live_badge.raise_()

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
