"""Encapsulate the long-press preview behaviour for asset grids."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import QModelIndex, QObject, QRect

from ..models.asset_model import Roles
from ..widgets.asset_grid import AssetGrid
from ..widgets.preview_window import PreviewWindow


class PreviewController(QObject):
    """Manage preview requests originating from gallery widgets."""

    def __init__(self, preview_window: PreviewWindow, parent: QObject | None = None) -> None:
        """Store the shared preview window instance."""

        super().__init__(parent)
        self._preview_window = preview_window

    def bind_view(self, view: AssetGrid) -> None:
        """Attach preview signal handlers to *view*."""

        view.requestPreview.connect(partial(self._handle_request_preview, view))
        view.previewReleased.connect(self.close_preview_after_release)
        view.previewCancelled.connect(self.cancel_preview)

    def close_preview(self, delayed: bool = True) -> None:
        """Close the preview window, optionally cancelling the delay timer."""

        self._preview_window.close_preview(delayed)

    def close_preview_after_release(self) -> None:
        """Hide the preview window after a successful long press."""

        self.close_preview(True)

    def cancel_preview(self) -> None:
        """Abort a preview that was cancelled before finishing the gesture."""

        self.close_preview(False)

    def _handle_request_preview(self, view: AssetGrid, index: QModelIndex) -> None:
        """Show the preview for *index* if it represents playable media."""

        if not index or not index.isValid():
            return
        is_video = bool(index.data(Roles.IS_VIDEO))
        is_live = bool(index.data(Roles.IS_LIVE))
        if not is_video and not is_live:
            return
        preview_raw = None
        if is_live:
            preview_raw = index.data(Roles.LIVE_MOTION_ABS)
        else:
            preview_raw = index.data(Roles.ABS)
        if not preview_raw:
            return
        preview_path = Path(str(preview_raw))
        rect = view.visualRect(index)
        global_rect = QRect(view.viewport().mapToGlobal(rect.topLeft()), rect.size())
        self._preview_window.show_preview(preview_path, global_rect)
