"""Focused tests for the floating info panel widget."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for GUI tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)

from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.gui.ui.widgets.info_panel import InfoPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Ensure a single QApplication instance exists for widget tests."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_info_panel_formats_video_metadata(qapp: QApplication) -> None:
    """Verify that video-specific fields render with human readable text."""

    panel = InfoPanel()
    metadata = {
        "rel": "clip.MOV",
        "name": "clip.MOV",
        "dt": "2024-02-18T12:34:56Z",
        "make": "Apple",
        "model": "Apple iPhone 13 Pro",
        "is_video": True,
        "w": 1920,
        "h": 1080,
        "bytes": 24192000,
        "codec": "hevc",
        "frame_rate": 59.94,
        "dur": 8.0,
    }

    panel.set_asset_metadata(metadata)

    assert panel.current_rel() == "clip.MOV"
    assert panel._camera_label.text() == "Apple iPhone 13 Pro"
    summary_text = panel._summary_label.text()
    assert "1920 × 1080" in summary_text
    assert "23.1 MB" in summary_text
    assert "HEVC" in summary_text
    details_text = panel._exposure_label.text()
    assert "fps" in details_text
    assert "0:08" in details_text
    assert not panel._lens_label.isVisible()
    panel.close()


def test_info_panel_video_missing_details_shows_fallback(qapp: QApplication) -> None:
    """When metadata is sparse the video fallback string should be displayed."""

    panel = InfoPanel()
    metadata = {
        "rel": "clip.MOV",
        "name": "clip.MOV",
        "is_video": True,
    }

    panel.set_asset_metadata(metadata)

    assert panel._exposure_label.text() == "Detailed video information is unavailable."
    assert not panel._summary_label.isVisible()
    panel.close()
