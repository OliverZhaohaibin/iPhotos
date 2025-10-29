"""Controller that encapsulates the gallery context menu logic."""

from __future__ import annotations

import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QCoreApplication, QMimeData, QObject, QPoint, QUrl, Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QMenu

from ...facade import AppFacade
from ..models.asset_model import AssetModel, Roles
from ..widgets.asset_grid import AssetGrid
from ..widgets.notification_toast import NotificationToast
from .navigation_controller import NavigationController
from .selection_controller import SelectionController
from .status_bar_controller import StatusBarController


class ContextMenuController(QObject):
    """Manage the asset grid context menu and related clipboard interactions."""

    def __init__(
        self,
        *,
        grid_view: AssetGrid,
        asset_model: AssetModel,
        facade: AppFacade,
        navigation: NavigationController,
        status_bar: StatusBarController,
        notification_toast: NotificationToast,
        selection_controller: SelectionController,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._grid_view = grid_view
        self._asset_model = asset_model
        self._facade = facade
        self._navigation = navigation
        self._status_bar = status_bar
        self._toast = notification_toast
        self._selection_controller = selection_controller

        self._grid_view.customContextMenuRequested.connect(self._handle_context_menu)

    # ------------------------------------------------------------------
    # Context menu workflow
    # ------------------------------------------------------------------
    def _handle_context_menu(self, point: QPoint) -> None:
        """Construct and display a context menu based on where the user right-clicked."""

        index = self._grid_view.indexAt(point)
        menu = QMenu(self._grid_view)
        # Menus inherit ``WA_TranslucentBackground`` from the frameless window shell.  The flag is
        # essential for rendering rounded corners, so we keep it enabled and rely on the palette-
        # driven stylesheet to paint an opaque surface that prevents any wallpaper bleed-through.
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setAutoFillBackground(True)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Popup
        )

        main_window = self._grid_view.window()
        if main_window is not None:
            # Copy the main window palette verbatim so the popup colours stay consistent across
            # multiple monitors and theme transitions.  The ``Base`` role keeps the surface opaque
            # so the rounded outline never reveals the desktop wallpaper beneath the menu.
            menu.setPalette(main_window.palette())
            menu.setBackgroundRole(QPalette.ColorRole.Base)

            stylesheet_accessor = getattr(main_window, "get_qmenu_stylesheet", None)
            stylesheet: Optional[str]
            if callable(stylesheet_accessor):
                stylesheet = stylesheet_accessor()
            else:
                fallback_accessor = getattr(main_window, "menu_stylesheet", None)
                stylesheet = fallback_accessor() if callable(fallback_accessor) else None

            if isinstance(stylesheet, str) and stylesheet:
                menu.setStyleSheet(stylesheet)

        # Clear any residual graphics effect in case another component previously attached one.
        # Removing stale ``QGraphicsEffect`` instances keeps the rounded outline crisp and avoids
        # blending artefacts that might otherwise leak in from other widgets.
        menu.setGraphicsEffect(None)

        selection_model = self._grid_view.selectionModel()

        # When the cursor is above an already selected item we expose actions that operate on
        # the selection (copy, reveal, move). This mirrors native file explorer conventions and
        # ensures that selection mode is not a prerequisite for quick actions.
        if index.isValid() and selection_model and selection_model.isSelected(index):
            copy_action = menu.addAction(
                QCoreApplication.translate("MainWindow", "Copy")
            )
            reveal_action = menu.addAction(
                QCoreApplication.translate(
                    "MainWindow", "Reveal in File Manager"
                )
            )
            move_menu = menu.addMenu(
                QCoreApplication.translate("MainWindow", "Move to")
            )
            delete_action = menu.addAction(
                QCoreApplication.translate("MainWindow", "Delete")
            )

            destinations = self._collect_move_targets()
            if destinations:
                for label, path in destinations:
                    action = move_menu.addAction(label)
                    action.triggered.connect(
                        partial(self._execute_move_to_album, path)
                    )
            else:
                move_menu.setEnabled(False)

            copy_action.triggered.connect(self._copy_selection_to_clipboard)
            reveal_action.triggered.connect(self._reveal_selection_in_file_manager)
            is_recently_deleted = self._navigation.is_recently_deleted_view()
            if is_recently_deleted:
                delete_action.setVisible(False)
                move_menu.menuAction().setVisible(False)
                restore_action = menu.addAction(
                    QCoreApplication.translate("MainWindow", "Restore")
                )
                restore_action.triggered.connect(self._execute_restore)
            else:
                delete_action.triggered.connect(self.delete_selection)

        # When the user invokes the context menu over an empty area we show album level actions
        # so that the gallery still offers meaningful commands while nothing is selected.
        else:
            paste_action = menu.addAction(
                QCoreApplication.translate("MainWindow", "Paste")
            )
            open_folder_action = menu.addAction(
                QCoreApplication.translate("MainWindow", "Open Folder Location")
            )

            paste_action.triggered.connect(self._paste_from_clipboard)
            open_folder_action.triggered.connect(self._open_current_folder)

        global_pos = self._grid_view.viewport().mapToGlobal(point)
        menu.exec(global_pos)

    def delete_selection(self) -> bool:
        """Move the current selection into the deleted-items collection."""

        if self._navigation.is_recently_deleted_view():
            self._status_bar.show_message(
                "Items inside Recently Deleted cannot be deleted again.",
                3000,
            )
            return False

        selection_model = self._grid_view.selectionModel()
        selected_indexes = (
            list(selection_model.selectedIndexes()) if selection_model else []
        )
        paths = self._selected_asset_paths()
        if not paths:
            self._status_bar.show_message("Select items to delete first.", 3000)
            return False

        source_model = self._asset_model.source_model()
        if selected_indexes:
            source_model.remove_rows(selected_indexes)

        try:
            self._facade.delete_assets(paths)
        except Exception:
            # Rescanning the album restores the rows we removed optimistically.
            self._facade.rescan_current()
            raise
        finally:
            self._selection_controller.set_selection_mode(False)

        self._toast.show_toast("Deleted")
        return True

    def _execute_restore(self) -> None:
        """Restore the current selection to the original albums recorded in the index."""

        selection_model = self._grid_view.selectionModel()
        selected_indexes = (
            list(selection_model.selectedIndexes()) if selection_model else []
        )
        paths = self._selected_asset_paths()
        if not paths:
            self._status_bar.show_message("Select items to restore first.", 3000)
            return

        source_model = self._asset_model.source_model()
        if selected_indexes:
            source_model.remove_rows(selected_indexes)

        try:
            self._facade.restore_assets(paths)
        except Exception:
            self._facade.rescan_current()
            raise
        finally:
            self._selection_controller.set_selection_mode(False)

        self._toast.show_toast("Restoring ...")

    def _copy_selection_to_clipboard(self) -> None:
        """Copy the selected asset file paths into the system clipboard."""

        paths = self._selected_asset_paths()
        if not paths:
            self._status_bar.show_message("Select items to copy first.", 3000)
            return
        existing = [path for path in paths if path.exists()]
        if not existing:
            self._status_bar.show_message(
                "Selected files are unavailable on disk.",
                3000,
            )
            return
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(str(path)) for path in existing])
        QGuiApplication.clipboard().setMimeData(mime_data)
        self._toast.show_toast("Copied to Clipboard")

    def _reveal_selection_in_file_manager(self) -> None:
        """Open the desktop file manager pointing to the first selected asset."""

        paths = self._selected_asset_paths()
        if not paths:
            self._status_bar.show_message("Select items to reveal first.", 3000)
            return

        path = paths[0]
        if not path.exists():
            self._status_bar.show_message(f"File not found: {path.name}", 3000)
            return

        # The command used to reveal a file varies per operating system. Each branch uses the
        # platform native tool to either highlight the file (Windows, macOS) or open the folder
        # containing the file (Linux and other POSIX systems).
        if sys.platform == "win32":
            subprocess.run(["explorer", "/select,", str(path)], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path.parent)], check=False)

        self._status_bar.show_message(
            f"Revealed {path.name} in file manager.",
            3000,
        )

    def _paste_from_clipboard(self) -> None:
        """Import files referenced in the clipboard into the currently opened album."""

        clipboard = QGuiApplication.clipboard()
        mime_data = clipboard.mimeData()
        if not mime_data.hasUrls():
            self._status_bar.show_message("No files to paste from clipboard.", 3000)
            return

        files = [Path(url.toLocalFile()) for url in mime_data.urls()]
        album = self._facade.current_album
        if not album:
            self._status_bar.show_message("Open an album before pasting files.", 3000)
            return

        # Delegate importing to the facade so that all deduplication and bookkeeping logic is
        # reused. The toast provides quick feedback because importing can take a noticeable
        # amount of time on large selections.
        self._facade.import_files(files, destination=album.root)
        self._toast.show_toast("Pasting files...")

    def _open_current_folder(self) -> None:
        """Open the current album folder in the desktop file manager."""

        album = self._facade.current_album
        if not album:
            self._status_bar.show_message("No album is currently open.", 3000)
            return

        path = album.root
        if not path.exists():
            self._status_bar.show_message(f"Folder not found: {path}", 3000)
            return

        # The command mirrors the implementation in ``_reveal_selection_in_file_manager`` but we
        # open the folder itself, not a specific file.
        if sys.platform == "win32":
            subprocess.run(["explorer", str(path)], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)

    def _execute_move_to_album(self, target: Path) -> None:
        """Move the currently selected assets to ``target`` while updating the view."""

        paths = self._selected_asset_paths()
        if not paths:
            self._status_bar.show_message("Select items to move first.", 3000)
            return

        try:
            self._facade.move_assets(paths, target)
        except Exception:
            self._asset_model.source_model().rollback_pending_moves()
            raise
        finally:
            self._selection_controller.set_selection_mode(False)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _selected_asset_paths(self) -> list[Path]:
        """Return absolute paths for all selected assets without duplicates."""

        selection_model = self._grid_view.selectionModel()
        if selection_model is None:
            return []
        seen: set[Path] = set()
        paths: list[Path] = []
        for index in selection_model.selectedIndexes():
            raw_path = index.data(Roles.ABS)
            if not raw_path:
                continue
            path = Path(str(raw_path))
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
        return paths

    def _collect_move_targets(self) -> list[tuple[str, Path]]:
        """Build a list of (label, path) destinations excluding the currently open album."""

        model = self._navigation.sidebar_model()
        entries = model.iter_album_entries()
        current_album = self._facade.current_album
        current_root: Path | None = None
        if current_album is not None:
            try:
                current_root = current_album.root.resolve()
            except OSError:
                current_root = current_album.root

        destinations: list[tuple[str, Path]] = []
        for label, path in entries:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if current_root is not None and resolved == current_root:
                continue
            destinations.append((label, path))
        return destinations
