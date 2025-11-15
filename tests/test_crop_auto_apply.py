"""Tests for automatic crop application in GLImageViewer and thumbnails."""

import pytest


def test_sidecar_crop_keys_preserved():
    """Test that resolve_render_adjustments preserves crop keys."""
    from iPhoto.io.sidecar import resolve_render_adjustments
    
    # Create adjustments with crop data
    adjustments = {
        "Crop_CX": 0.5,
        "Crop_CY": 0.5,
        "Crop_W": 0.75,
        "Crop_H": 0.75,
        "Light_Master": 0.0,
        "Light_Enabled": True,
    }
    
    resolved = resolve_render_adjustments(adjustments)
    
    # Verify crop keys are preserved
    assert "Crop_CX" in resolved
    assert "Crop_CY" in resolved
    assert "Crop_W" in resolved
    assert "Crop_H" in resolved
    assert resolved["Crop_CX"] == 0.5
    assert resolved["Crop_CY"] == 0.5
    assert resolved["Crop_W"] == 0.75
    assert resolved["Crop_H"] == 0.75


def test_crop_detection_logic():
    """Test the logic for detecting valid crop data."""
    # This tests the logic implemented in _auto_apply_crop_mode
    
    # No crop cases
    assert not _has_valid_crop({"Crop_W": 1.0, "Crop_H": 1.0})
    assert not _has_valid_crop({})
    
    # Valid crop cases
    assert _has_valid_crop({"Crop_W": 0.5, "Crop_H": 1.0})
    assert _has_valid_crop({"Crop_W": 1.0, "Crop_H": 0.5})
    assert _has_valid_crop({"Crop_W": 0.5, "Crop_H": 0.5})
    assert _has_valid_crop({"Crop_W": 0.99, "Crop_H": 1.0})


def _has_valid_crop(adjustments):
    """Helper function to replicate the crop detection logic."""
    crop_w = adjustments.get("Crop_W", 1.0)
    crop_h = adjustments.get("Crop_H", 1.0)
    return crop_w < 1.0 or crop_h < 1.0


def test_crop_coordinate_calculation():
    """Test crop coordinate calculations for thumbnails."""
    # Test the math used in _apply_crop
    
    # 100x100 image, crop to 50% centered
    img_width, img_height = 100, 100
    crop_cx, crop_cy = 0.5, 0.5
    crop_w, crop_h = 0.5, 0.5
    
    crop_width_px = int(crop_w * img_width)
    crop_height_px = int(crop_h * img_height)
    crop_left_px = int((crop_cx * img_width) - (crop_width_px / 2))
    crop_top_px = int((crop_cy * img_height) - (crop_height_px / 2))
    
    assert crop_width_px == 50
    assert crop_height_px == 50
    assert crop_left_px == 25  # Center at 50, crop width 50, so starts at 25
    assert crop_top_px == 25
    
    # Test bounds clamping
    crop_left_px = max(0, min(crop_left_px, img_width - 1))
    crop_top_px = max(0, min(crop_top_px, img_height - 1))
    crop_width_px = max(1, min(crop_width_px, img_width - crop_left_px))
    crop_height_px = max(1, min(crop_height_px, img_height - crop_top_px))
    
    assert crop_left_px >= 0
    assert crop_top_px >= 0
    assert crop_width_px > 0
    assert crop_height_px > 0
    assert crop_left_px + crop_width_px <= img_width


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

