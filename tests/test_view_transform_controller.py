"""Test suite for ViewTransformController and crop overlay behavior fixes."""

from __future__ import annotations

import os
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6", reason="PySide6 required for UI tests", exc_type=ImportError)

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from iPhoto.gui.ui.widgets.view_transform_controller import (
    ViewTransformController,
    compute_fit_to_view_scale,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Create a QApplication instance for testing."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def mock_viewer():
    """Create a mock viewer widget for testing."""
    viewer = Mock()
    viewer.width.return_value = 800
    viewer.height.return_value = 600
    viewer.devicePixelRatioF.return_value = 1.0
    viewer.update = Mock()
    return viewer


@pytest.fixture
def controller(mock_viewer):
    """Create a ViewTransformController with a mock viewer."""
    texture_size_provider = Mock(return_value=(1920, 1080))
    on_zoom_changed = Mock()
    
    ctrl = ViewTransformController(
        mock_viewer,
        texture_size_provider=texture_size_provider,
        on_zoom_changed=on_zoom_changed,
    )
    return ctrl


def test_compute_fit_to_view_scale_landscape():
    """Test fit-to-view calculation for landscape images."""
    # Landscape image (1920x1080) in landscape viewport (800x600)
    scale = compute_fit_to_view_scale((1920, 1080), 800.0, 600.0)
    # Should fit by height: 600/1080 ≈ 0.556
    assert 0.55 < scale < 0.56


def test_compute_fit_to_view_scale_portrait():
    """Test fit-to-view calculation for portrait images."""
    # Portrait image (1080x1920) in landscape viewport (800x600)
    scale = compute_fit_to_view_scale((1080, 1920), 800.0, 600.0)
    # Should fit by height: 600/1920 ≈ 0.3125
    assert 0.31 < scale < 0.32


def test_center_on_screen_point_moves_point_to_center(controller, mock_viewer):
    """Test that center_on_screen_point correctly pans to move a point to center."""
    # Initial state: no pan
    assert controller.get_pan_pixels() == QPointF(0.0, 0.0)
    
    # Point at top-left corner of viewport (Qt coordinates: top-left origin)
    point = QPointF(0.0, 0.0)
    
    # Center this point (should require positive pan in both axes)
    controller.center_on_screen_point(point)
    
    # After centering, the pan should move the top-left to the center
    pan = controller.get_pan_pixels()
    # Expected: move by (-400, +300) in bottom-left coords to center the point
    # (Qt top-left to GL bottom-left conversion)
    assert pan.x() < 0  # Pan left to bring point right
    assert pan.y() > 0  # Pan up to bring point down


def test_center_on_screen_point_already_centered(controller):
    """Test centering a point that's already at the viewport center."""
    # Point already at center
    center_point = QPointF(400.0, 300.0)  # 800x600 viewport
    
    controller.center_on_screen_point(center_point)
    
    # Pan should be near zero (point already centered)
    pan = controller.get_pan_pixels()
    assert abs(pan.x()) < 1.0
    assert abs(pan.y()) < 1.0


def test_center_on_screen_point_with_existing_pan(controller):
    """Test centering when there's already a pan offset."""
    # Set initial pan
    controller.set_pan_pixels(QPointF(100.0, 50.0))
    
    # Try to center a point
    point = QPointF(200.0, 200.0)
    controller.center_on_screen_point(point)
    
    # The pan should have changed to center the point
    pan = controller.get_pan_pixels()
    assert pan != QPointF(100.0, 50.0)


def test_zoom_and_pan_interaction(controller):
    """Test that zoom and pan work together correctly."""
    # Set some zoom
    controller.set_zoom(2.0)
    assert controller.get_zoom_factor() == 2.0
    
    # Pan to a position
    controller.set_pan_pixels(QPointF(50.0, 50.0))
    assert controller.get_pan_pixels() == QPointF(50.0, 50.0)
    
    # Center a point - should only affect pan, not zoom
    controller.center_on_screen_point(QPointF(100.0, 100.0))
    assert controller.get_zoom_factor() == 2.0  # Zoom unchanged
    assert controller.get_pan_pixels() != QPointF(50.0, 50.0)  # Pan changed


def test_center_view_zeros_pan(controller):
    """Test that center_view resets pan to zero."""
    # Set some pan
    controller.set_pan_pixels(QPointF(100.0, 200.0))
    
    # Center view should zero the pan
    controller.center_view()
    
    pan = controller.get_pan_pixels()
    assert pan.x() == 0.0
    assert pan.y() == 0.0


def test_reset_zoom_centers_and_zooms_to_one(controller):
    """Test that reset_zoom restores baseline state."""
    # Change zoom and pan
    controller.set_zoom(3.0)
    controller.set_pan_pixels(QPointF(100.0, 100.0))
    
    # Reset should restore to zoom=1.0 and pan=(0, 0)
    controller.reset_zoom()
    
    assert controller.get_zoom_factor() == 1.0
    assert controller.get_pan_pixels() == QPointF(0.0, 0.0)
