import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for GUI tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)
pytest.importorskip("PySide6.QtGui", reason="Qt GUI not available", exc_type=ImportError)
pytest.importorskip("PySide6.QtTest", reason="Qt test utilities unavailable", exc_type=ImportError)

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.config import LONG_PRESS_THRESHOLD_MS
from iPhotos.src.iPhoto.gui.ui.widgets.asset_grid import AssetGrid


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_asset_grid_long_press_emits_preview(qapp: QApplication) -> None:
    grid = AssetGrid()
    model = QStandardItemModel()
    model.appendRow(QStandardItem("item"))
    grid.setModel(model)
    grid.setIconSize(grid.iconSize())  # ensure layout initialises
    grid.show()
    qapp.processEvents()

    index = model.index(0, 0)
    rect = grid.visualRect(index)
    pos = rect.center()

    preview_spy = QSignalSpy(grid.requestPreview)
    release_spy = QSignalSpy(grid.previewReleased)

    QTest.mousePress(grid.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert preview_spy.wait(LONG_PRESS_THRESHOLD_MS + 800)

    qapp.processEvents()
    global_pos = grid.viewport().mapToGlobal(pos)
    target = QApplication.widgetAt(global_pos)
    if target is None:
        target = grid.viewport()
        local_pos = pos
    else:
        local_pos = target.mapFromGlobal(global_pos)

    QTest.mouseRelease(target, Qt.MouseButton.LeftButton, pos=local_pos)
    if release_spy.count() == 0:
        assert release_spy.wait(800)

    assert release_spy.count() > 0
