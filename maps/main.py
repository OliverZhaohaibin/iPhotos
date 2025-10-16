"""Entry point for the PySide6 based vector tile preview application."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QOffscreenSurface, QOpenGLContext
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox

from map_widget import MapGLWidget, MapWidget
from map_widget._map_widget_base import MapWidgetBase
from style_resolver import StyleLoadError
from tile_parser import TileLoadingError


def check_opengl_support() -> bool:
    """Return ``True`` when the system can create a basic OpenGL context."""

    try:
        surface = QOffscreenSurface()
        surface.create()
        if not surface.isValid():
            return False

        context = QOpenGLContext()
        if not context.create():
            return False

        if not context.makeCurrent(surface):
            return False

        context.doneCurrent()
        return True
    except Exception:
        # Any failure means the platform cannot offer the required OpenGL
        # features, so we fall back to the CPU implementation.
        return False


class MainWindow(QMainWindow):
    """Primary application window that hosts an interactive map widget."""

    def __init__(
        self,
        tile_root: str = "tiles",
        style_path: str = "style.json",
        *,
        widget_class: type[MapWidgetBase] | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Map Preview")
        self.resize(1024, 768)

        self._tile_root = tile_root
        self._style_path = style_path

        self._widget_cls: type[MapWidgetBase] = widget_class or MapWidget
        self._map_widget: MapWidgetBase = self._create_map_widget(
            tile_root=self._tile_root,
            style_path=self._style_path,
        )
        self._set_central_map(self._map_widget)

        self._create_actions()
        self._create_menus()
        self._update_window_title()

    # ------------------------------------------------------------------
    def _create_actions(self) -> None:
        """Assemble actions that appear in the menu bar."""

        self._action_zoom_in = QAction("Zoom In", self)
        self._action_zoom_in.setShortcut(Qt.Key_Plus)
        self._action_zoom_in.triggered.connect(self._zoom_in)

        self._action_zoom_out = QAction("Zoom Out", self)
        self._action_zoom_out.setShortcut(Qt.Key_Minus)
        self._action_zoom_out.triggered.connect(self._zoom_out)

        self._action_open_style = QAction("Load Style…", self)
        self._action_open_style.triggered.connect(self._open_style)

        self._action_open_tiles = QAction("Select Tile Directory…", self)
        self._action_open_tiles.triggered.connect(self._open_tile_directory)

        self._action_reset_view = QAction("Reset View", self)
        self._action_reset_view.triggered.connect(self._reset_view)

    # ------------------------------------------------------------------
    def _create_menus(self) -> None:
        """Create the menu structure shown in the window."""

        menu_bar = self.menuBar()

        view_menu = menu_bar.addMenu("View")
        view_menu.addAction(self._action_zoom_in)
        view_menu.addAction(self._action_zoom_out)
        view_menu.addSeparator()
        view_menu.addAction(self._action_reset_view)

        file_menu = menu_bar.addMenu("File")
        file_menu.addAction(self._action_open_style)
        file_menu.addAction(self._action_open_tiles)

    # ------------------------------------------------------------------
    def _create_map_widget(self, *, tile_root: str, style_path: str) -> MapWidgetBase:
        """Instantiate the preferred widget class with CPU fallback."""

        try:
            widget = self._widget_cls(tile_root=tile_root, style_path=style_path)
        except (StyleLoadError, TileLoadingError):
            # Style and tile loading errors should propagate so the caller can
            # provide a more helpful error dialog.
            raise
        except Exception as exc:  # pragma: no cover - best effort error reporting
            if self._widget_cls is MapWidget:
                raise

            QMessageBox.warning(
                self,
                "GPU Acceleration Disabled",
                "The OpenGL based map view failed to initialize.\n"
                "The application will continue with the CPU renderer instead.\n\n"
                f"Details: {exc}",
            )
            self._widget_cls = MapWidget
            widget = MapWidget(tile_root=tile_root, style_path=style_path)
        return widget

    # ------------------------------------------------------------------
    def _zoom_in(self) -> None:
        """Increase the zoom level using a multiplicative factor."""

        # Multiplying the zoom level produces smoother transitions than a
        # single integer increment, especially at high zooms where ``+1`` would
        # represent an enormous scale jump.  The factor of ``1.5`` matches the
        # gentle ramp used for wheel-based zooming.
        self._map_widget.set_zoom(self._map_widget.zoom * 1.5)
        self._update_window_title()

    # ------------------------------------------------------------------
    def _zoom_out(self) -> None:
        """Decrease the zoom level while maintaining smooth transitions."""

        # Dividing by the same factor keeps zoom in/out symmetric so repeated
        # key presses return to the original scale without drift from rounding.
        self._map_widget.set_zoom(self._map_widget.zoom / 1.5)
        self._update_window_title()

    # ------------------------------------------------------------------
    def _reset_view(self) -> None:
        """Re-center the map and return to the default zoom level."""

        self._map_widget.reset_view()
        self._update_window_title()

    # ------------------------------------------------------------------
    def _open_style(self) -> None:
        """Allow the user to select a different ``style.json`` file."""

        path, _ = QFileDialog.getOpenFileName(self, "Select style.json", self._style_path, "JSON Files (*.json)")
        if not path:
            return

        try:
            widget = self._create_map_widget(tile_root=self._tile_root, style_path=path)
        except StyleLoadError as exc:  # pragma: no cover - best effort error reporting
            QMessageBox.critical(self, "Error", f"Unable to load the style file:\n{exc}")
            return
        except TileLoadingError as exc:  # pragma: no cover - best effort error reporting
            QMessageBox.critical(self, "Error", f"Unable to initialize tiles:\n{exc}")
            return

        self._style_path = path
        self._set_central_map(widget)
        self._update_window_title()

    # ------------------------------------------------------------------
    def _open_tile_directory(self) -> None:
        """Allow the user to switch to a different tile directory."""

        path = QFileDialog.getExistingDirectory(self, "Select tile directory", self._tile_root)
        if not path:
            return

        try:
            widget = self._create_map_widget(tile_root=path, style_path=self._style_path)
        except StyleLoadError as exc:  # pragma: no cover - best effort error reporting
            QMessageBox.critical(self, "Error", f"Unable to load the style file:\n{exc}")
            return
        except TileLoadingError as exc:  # pragma: no cover - best effort error reporting
            QMessageBox.critical(self, "Error", f"Unable to open the tile directory:\n{exc}")
            return

        self._tile_root = path
        self._set_central_map(widget)
        self._update_window_title()

    # ------------------------------------------------------------------
    def _update_window_title(self) -> None:
        """Include the current zoom level in the window title."""

        self.setWindowTitle(f"Map Preview — Zoom {self._map_widget.zoom:.2f}")

    # ------------------------------------------------------------------
    def _set_central_map(self, widget: MapWidgetBase) -> None:
        """Replace the current central widget with ``widget`` safely."""

        old = self.takeCentralWidget()
        if old is not None:
            if hasattr(old, "shutdown"):
                old.shutdown()  # type: ignore[call-arg]
            old.deleteLater()
        self._map_widget = widget
        self.setCentralWidget(self._map_widget)


def main() -> int:
    """Application entry point used by ``python main.py``."""

    app = QApplication(sys.argv)

    # Prefer the GPU accelerated widget when possible because it removes a
    # substantial amount of per-frame work from the CPU.  Falling back to the
    # software widget keeps the application functional on systems that lack
    # OpenGL support, but users should expect a higher CPU load in that mode.
    use_opengl = check_opengl_support()
    widget_cls: type[MapWidgetBase] = MapGLWidget if use_opengl else MapWidget
    if use_opengl:
        print("OpenGL support detected. Using GPU accelerated rendering.")
    else:
        print("OpenGL support unavailable. Falling back to CPU rendering.")

    try:
        window = MainWindow(widget_class=widget_cls)
    except (StyleLoadError, TileLoadingError) as exc:
        QMessageBox.critical(None, "Error", f"Failed to initialize map:\n{exc}")
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
