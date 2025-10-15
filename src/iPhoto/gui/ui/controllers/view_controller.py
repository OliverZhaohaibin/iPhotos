"""Helpers for switching between the gallery and detail pages."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QStackedWidget, QWidget


class ViewController(QObject):
    """Manage transitions between the main gallery and detail views."""

    galleryViewShown = Signal()
    """Signal emitted after the gallery view becomes the active page."""

    detailViewShown = Signal()
    """Signal emitted after the detail view becomes the active page."""

    def __init__(
        self,
        view_stack: QStackedWidget,
        gallery_page: QWidget | None,
        detail_page: QWidget | None,
        map_page: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        """Initialise the controller with the stacked widget and its pages."""

        super().__init__(parent)
        self._view_stack = view_stack
        self._gallery_page = gallery_page
        self._detail_page = detail_page
        self._map_page = map_page
        self._active_gallery_page = gallery_page

    def show_gallery_view(self) -> None:
        """Switch to the gallery view and notify listeners."""

        target = self._active_gallery_page or self._gallery_page
        if target is not None:
            if self._view_stack.currentWidget() is not target:
                self._view_stack.setCurrentWidget(target)
        self.galleryViewShown.emit()

    def show_detail_view(self) -> None:
        """Switch to the detail view and notify listeners."""

        if self._detail_page is not None:
            if self._view_stack.currentWidget() is not self._detail_page:
                self._view_stack.setCurrentWidget(self._detail_page)
        self.detailViewShown.emit()

    def show_map_view(self) -> None:
        """Switch to the map page and treat it as the active gallery view."""

        if self._map_page is None:
            return
        self._active_gallery_page = self._map_page
        if self._view_stack.currentWidget() is not self._map_page:
            self._view_stack.setCurrentWidget(self._map_page)
        self.galleryViewShown.emit()

    def restore_default_gallery(self) -> None:
        """Reset the gallery view back to the standard grid."""

        self._active_gallery_page = self._gallery_page
