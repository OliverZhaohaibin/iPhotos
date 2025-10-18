"""Controller that encapsulates the gallery context menu logic."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPoint, QCoreApplication, QUrl, QMimeData
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu

from ...facade import AppFacade
from ..models.asset_model import AssetModel, Roles
from ..widgets.asset_grid import AssetGrid
from ..widgets.notification_toast import NotificationToast
from .navigation_controller import NavigationController
from .selection_controller import SelectionController
from .status_bar_controller import StatusBarController


class ContextMenuController(QObject):
    """Present copy and move actions when selection mode is active."""

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
        if not self._selection_controller.is_active():
            return

        selection_model = self._grid_view.selectionModel()
        if selection_model is None or not selection_model.selectedIndexes():
            return

        index = self._grid_view.indexAt(point)
        if not index.isValid() or not selection_model.isSelected(index):
            return

        menu = QMenu(self._grid_view)
        copy_action = menu.addAction(
            QCoreApplication.translate("MainWindow", "Copy")
        )
        move_menu = menu.addMenu(
            QCoreApplication.translate("MainWindow", "Move to")
        )

        destinations = self._collect_move_targets()
        if destinations:
            for label, path in destinations:
                action = move_menu.addAction(label)
                action.triggered.connect(partial(self._execute_move_to_album, path))
        else:
            move_menu.setEnabled(False)

        copy_action.triggered.connect(self._copy_selection_to_clipboard)
        global_pos = self._grid_view.viewport().mapToGlobal(point)
        menu.exec(global_pos)

    def _copy_selection_to_clipboard(self) -> None:
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

    def _execute_move_to_album(self, target: Path) -> None:
        selection_model = self._grid_view.selectionModel()
        selected_indexes = (
            list(selection_model.selectedIndexes()) if selection_model else []
        )
        paths = self._selected_asset_paths()
        if not paths:
            self._status_bar.show_message("Select items to move first.", 3000)
            return

        source_model = self._asset_model.source_model()
        is_virtual_view_move = self._navigation.is_basic_library_virtual_view()
        if is_virtual_view_move:
            rels = [
                index.data(Roles.REL)
                for index in selected_indexes
                if index.isValid()
            ]
            source_model.update_rows_for_move(
                [rel for rel in rels if isinstance(rel, str)], target
            )
        elif selected_indexes:
            source_model.remove_rows(selected_indexes)

        try:
            self._facade.move_assets(paths, target)
        except Exception:
            source_model.rollback_pending_moves()
            raise
        finally:
            self._selection_controller.set_selection_mode(False)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _selected_asset_paths(self) -> list[Path]:
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
