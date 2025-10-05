"""Dialog orchestration helpers for the main window."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QStatusBar, QWidget

# Allow both ``iPhoto.gui`` and legacy ``iPhotos.src.iPhoto.gui`` import paths.
try:  # pragma: no cover - depends on runtime packaging
    from ...appctx import AppContext
except ImportError:  # pragma: no cover - fallback for script execution
    from iPhoto.appctx import AppContext
from ...errors import LibraryError
from ..widgets import dialogs


class DialogController:
    """Centralise dialog and message interactions."""

    def __init__(self, parent: QWidget, context: AppContext, status_bar: QStatusBar) -> None:
        self._parent = parent
        self._context = context
        self._status = status_bar

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def open_album_dialog(self) -> Optional[Path]:
        return dialogs.select_directory(self._parent, "Select album")

    def bind_library_dialog(self) -> Optional[Path]:
        root = dialogs.select_directory(self._parent, "Select Basic Library")
        if root is None:
            return None
        try:
            self._context.library.bind_path(root)
        except LibraryError as exc:
            dialogs.show_error(self._parent, str(exc))
            return None
        bound_root = self._context.library.root()
        if bound_root is not None:
            self._context.settings.set("basic_library_path", str(bound_root))
            self._status.showMessage(f"Basic Library bound to {bound_root}")
        return bound_root

    def show_error(self, message: str) -> None:
        dialogs.show_error(self._parent, message)

    def prompt_for_basic_library(self) -> None:
        dialogs.show_information(
            self._parent,
            "Select a folder to use as your Basic Library.",
            title="Bind Basic Library",
        )
        self.bind_library_dialog()
