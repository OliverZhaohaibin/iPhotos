"""Regression tests for :mod:`iPhoto.gui.ui.controllers.navigation_controller`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import os
import pytest

pytest.importorskip(
    "PySide6",
    reason="PySide6 is required for navigation controller tests",
    exc_type=ImportError,
)
pytest.importorskip(
    "PySide6.QtWidgets",
    reason="Qt widgets not available",
    exc_type=ImportError,
)

from PySide6.QtWidgets import QApplication, QLabel, QStackedWidget, QStatusBar, QWidget

from iPhotos.src.iPhoto.gui.ui.controllers.navigation_controller import NavigationController
from iPhotos.src.iPhoto.gui.ui.controllers.view_controller import ViewController


@pytest.fixture
def qapp() -> QApplication:
    """Return the shared :class:`QApplication` instance for the test suite."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _SpyViewController(ViewController):
    """Spy variant that records gallery/detail transitions for assertions."""

    def __init__(self) -> None:
        self._stack = QStackedWidget()
        self._gallery = QWidget()
        self._detail = QWidget()
        self._stack.addWidget(self._gallery)
        self._stack.addWidget(self._detail)
        self.gallery_calls = 0
        self.detail_calls = 0
        super().__init__(self._stack, self._gallery, self._detail)

    def show_gallery_view(self) -> None:  # type: ignore[override]
        self.gallery_calls += 1
        super().show_gallery_view()

    def show_detail_view(self) -> None:  # type: ignore[override]
        self.detail_calls += 1
        super().show_detail_view()


class _StubFacade:
    """Minimal facade exposing the bits required by :class:`NavigationController`."""

    def __init__(self) -> None:
        self.current_album: Optional[SimpleNamespace] = None
        self.open_requests: list[Path] = []

    def open_album(self, root: Path) -> SimpleNamespace:
        self.open_requests.append(root)
        album = SimpleNamespace(root=root.resolve(), manifest={"title": root.name})
        self.current_album = album
        return album


class _StubAssetModel:
    """Track calls to ``set_filter_mode`` for sanity checks in tests."""

    def __init__(self) -> None:
        self.filter_mode = object()

    def set_filter_mode(self, mode: Optional[str]) -> None:
        self.filter_mode = mode

    def rowCount(self) -> int:  # pragma: no cover - unused but required by controller
        return 0


class _StubSidebar:
    """Sidebar stand-in that optionally calls back when selection changes."""

    def __init__(self) -> None:
        self._current_path: Optional[Path] = None
        self._callback: Optional[Callable[[Path], None]] = None

    def set_callback(self, callback: Callable[[Path], None]) -> None:
        self._callback = callback

    def select_path(self, path: Path) -> None:
        already_selected = self._current_path == path
        self._current_path = path
        if not already_selected and self._callback is not None:
            self._callback(path)

    def select_static_node(self, _title: str) -> None:
        # The refresh logic is exercised through ``select_path`` in the tests,
        # so there is nothing extra to do for static nodes here.
        return


class _StubContext:
    """Capture the last album remembered by :class:`NavigationController`."""

    def __init__(self, library_root: Path) -> None:
        self._library_root = library_root
        self.facade = None
        self.library = SimpleNamespace(root=lambda: self._library_root)
        self.remembered: Optional[Path] = None

    def remember_album(self, root: Path) -> None:
        self.remembered = root


class _StubDialog:
    """Placeholder dialog controller used to satisfy the constructor."""

    def bind_library_dialog(self) -> None:  # pragma: no cover - not exercised
        return


def test_open_album_skips_gallery_on_refresh(tmp_path: Path, qapp: QApplication) -> None:
    """Reopening the active album via sidebar sync must not reset the gallery."""

    facade = _StubFacade()
    context = _StubContext(tmp_path)
    context.facade = facade
    asset_model = _StubAssetModel()
    sidebar = _StubSidebar()
    album_label = QLabel()
    status_bar = QStatusBar()
    dialog = _StubDialog()
    view_controller = _SpyViewController()

    controller = NavigationController(
        context,
        facade,
        asset_model,
        sidebar,
        album_label,
        status_bar,
        dialog,  # type: ignore[arg-type]
        view_controller,
    )

    album_path = tmp_path / "album"
    album_path.mkdir()

    # Simulate the user selecting the album for the first time.  This should
    # present the gallery view so the model can populate cleanly.
    controller.open_album(album_path)
    assert view_controller.gallery_calls == 1
    assert controller.consume_last_open_refresh() is False
    assert facade.open_requests == [album_path]

    # ``handle_album_opened`` drives the sidebar selection update in the real
    # application.  Wire the stub to mimic the sidebar re-emitting
    # ``albumSelected`` so ``open_album`` receives a second call while the sync
    # flag is active.
    sidebar.set_callback(controller.open_album)
    controller.handle_album_opened(album_path)

    # The refresh triggered by the sidebar must not reset the gallery view and
    # should advertise itself via ``consume_last_open_refresh``.  Crucially, the
    # facade must not be asked to reload the already-open album again.
    assert view_controller.gallery_calls == 1
    assert controller.consume_last_open_refresh() is True
    assert facade.open_requests == [album_path]


def test_open_album_refresh_detected_without_sidebar_sync(
    tmp_path: Path, qapp: QApplication
) -> None:
    """A second ``open_album`` call for the same path must be treated as a refresh."""

    facade = _StubFacade()
    context = _StubContext(tmp_path)
    context.facade = facade
    asset_model = _StubAssetModel()
    sidebar = _StubSidebar()
    album_label = QLabel()
    status_bar = QStatusBar()
    dialog = _StubDialog()
    view_controller = _SpyViewController()

    controller = NavigationController(
        context,
        facade,
        asset_model,
        sidebar,
        album_label,
        status_bar,
        dialog,  # type: ignore[arg-type]
        view_controller,
    )

    album_path = tmp_path / "album"
    album_path.mkdir()

    # First call represents a genuine navigation and should reset the gallery.
    controller.open_album(album_path)
    assert view_controller.gallery_calls == 1
    assert controller.consume_last_open_refresh() is False
    assert facade.open_requests == [album_path]

    # The follow-up call mimics a filesystem watcher re-selecting the already
    # open album without going through ``handle_album_opened``.  The controller
    # should classify it as a refresh so the UI stays on the detail page.
    controller.open_album(album_path)

    assert view_controller.gallery_calls == 1
    assert controller.consume_last_open_refresh() is True
    assert facade.open_requests == [album_path]
