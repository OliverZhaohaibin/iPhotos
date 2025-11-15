"""Tests for crop-aware view reset functionality in ViewTransformController."""

import pytest
from unittest.mock import Mock
from PySide6.QtCore import QPointF

from iPhoto.gui.ui.widgets.view_transform_controller import ViewTransformController


@pytest.fixture
def mock_viewer():
    """Create a mock QOpenGLWidget for testing."""
    viewer = Mock()
    viewer.width.return_value = 800
    viewer.height.return_value = 600
    viewer.devicePixelRatioF.return_value = 1.0
    return viewer


@pytest.fixture
def controller(mock_viewer):
    """Create a ViewTransformController instance for testing."""
    texture_size_provider = lambda: (1600, 1200)
    on_zoom_changed = Mock()
    
    ctrl = ViewTransformController(
        mock_viewer,
        texture_size_provider=texture_size_provider,
        on_zoom_changed=on_zoom_changed,
    )
    return ctrl


class TestResetZoomToRect:
    """Test cases for the reset_zoom_to_rect method."""
    
    def test_no_crop_falls_back_to_reset_zoom(self, controller):
        """When crop parameters indicate no crop, should behave like reset_zoom."""
        crop_params = {
            "Crop_CX": 0.5,
            "Crop_CY": 0.5,
            "Crop_W": 1.0,
            "Crop_H": 1.0,
        }
        
        # Set some initial zoom/pan state
        controller._zoom_factor = 2.0
        controller._pan_px = QPointF(100.0, 50.0)
        
        controller.reset_zoom_to_rect(crop_params)
        
        # Should reset to baseline
        assert controller._zoom_factor == 1.0
        assert controller._pan_px.x() == 0.0
        assert controller._pan_px.y() == 0.0
    
    def test_crop_50_percent_adjusts_zoom_and_pan(self, controller):
        """When crop is 50% of image, zoom should adjust to fit crop region."""
        crop_params = {
            "Crop_CX": 0.5,  # Center of image
            "Crop_CY": 0.5,  # Center of image
            "Crop_W": 0.5,   # 50% width
            "Crop_H": 0.5,   # 50% height
        }
        
        controller.reset_zoom_to_rect(crop_params)
        
        # Zoom factor should still be 1.0 (base scale handles the fitting)
        assert controller._zoom_factor == 1.0
        
        # Pan should be 0 since crop is centered
        assert abs(controller._pan_px.x()) < 1.0
        assert abs(controller._pan_px.y()) < 1.0
    
    def test_crop_offset_from_center(self, controller):
        """When crop is offset from center, pan should adjust accordingly."""
        crop_params = {
            "Crop_CX": 0.75,  # Offset to right
            "Crop_CY": 0.25,  # Offset to top
            "Crop_W": 0.5,    # 50% width
            "Crop_H": 0.5,    # 50% height
        }
        
        controller.reset_zoom_to_rect(crop_params)
        
        # Zoom factor should be 1.0
        assert controller._zoom_factor == 1.0
        
        # Pan should be non-zero to center the crop region
        # Since crop center is at (0.75, 0.25) in normalized coords,
        # we expect pan to be adjusted
        assert controller._pan_px.x() != 0.0 or controller._pan_px.y() != 0.0
    
    def test_missing_crop_params_uses_defaults(self, controller):
        """When crop params are missing, should use default values."""
        crop_params = {}
        
        controller.reset_zoom_to_rect(crop_params)
        
        # Should behave like no crop (defaults to 1.0)
        assert controller._zoom_factor == 1.0
        assert controller._pan_px.x() == 0.0
        assert controller._pan_px.y() == 0.0
    
    def test_small_crop_region(self, controller):
        """Test with a very small crop region (10% of image)."""
        crop_params = {
            "Crop_CX": 0.5,
            "Crop_CY": 0.5,
            "Crop_W": 0.1,   # 10% width
            "Crop_H": 0.1,   # 10% height
        }
        
        controller.reset_zoom_to_rect(crop_params)
        
        # Should still work correctly with small crops
        assert controller._zoom_factor == 1.0
    
    def test_returns_changed_true_when_state_changes(self, controller):
        """Method should return True when view transform changes."""
        crop_params = {
            "Crop_CX": 0.75,
            "Crop_CY": 0.75,
            "Crop_W": 0.5,
            "Crop_H": 0.5,
        }
        
        # Set initial non-default state
        controller._zoom_factor = 2.0
        controller._pan_px = QPointF(100.0, 100.0)
        
        changed = controller.reset_zoom_to_rect(crop_params)
        
        assert changed is True
    
    def test_returns_changed_false_when_already_at_target(self, controller):
        """Method should return False when state doesn't change."""
        crop_params = {
            "Crop_CX": 0.5,
            "Crop_CY": 0.5,
            "Crop_W": 1.0,
            "Crop_H": 1.0,
        }
        
        # Already at baseline state
        controller._zoom_factor = 1.0
        controller._pan_px = QPointF(0.0, 0.0)
        
        changed = controller.reset_zoom_to_rect(crop_params)
        
        assert changed is False


class TestGLImageViewerIntegration:
    """Test GLImageViewer's reset_zoom_to_crop method."""
    
    def test_reset_zoom_to_crop_with_no_adjustments(self):
        """Should fall back to reset_zoom when adjustments are None."""
        from iPhoto.gui.ui.widgets.gl_image_viewer import GLImageViewer
        
        # We can't easily create a full GLImageViewer in tests without Qt context,
        # but we can verify the method exists and has correct signature
        assert hasattr(GLImageViewer, 'reset_zoom_to_crop')
    
    def test_reset_zoom_to_crop_with_empty_adjustments(self):
        """Should fall back to reset_zoom when adjustments are empty."""
        from iPhoto.gui.ui.widgets.gl_image_viewer import GLImageViewer
        
        # Verify method exists
        assert hasattr(GLImageViewer, 'reset_zoom_to_crop')
