from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for settings tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)
pytest.importorskip("PySide6.QtTest", reason="Qt test helpers not available", exc_type=ImportError)

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.settings.manager import SettingsManager


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_settings_manager_roundtrip(tmp_path: Path, qapp: QApplication) -> None:
    settings_path = tmp_path / "settings.json"
    manager = SettingsManager(path=settings_path)
    manager.load()
    assert settings_path.exists()
    assert manager.get("basic_library_path") is None
    spy = QSignalSpy(manager.settingsChanged)
    library_path = tmp_path / "Library"
    manager.set("basic_library_path", library_path)
    qapp.processEvents()
    assert spy.count() == 1
    assert manager.get("basic_library_path") == str(library_path)
    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert stored["basic_library_path"] == str(library_path)


def test_settings_manager_nested_updates_preserve_defaults(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    manager = SettingsManager(path=settings_path)
    manager.load()
    manager.set("ui.sidebar_width", 320)
    assert manager.get("ui.sidebar_width") == 320
    assert manager.get("ui.theme") == "light"
