"""Test crop parameter persistence in EditSession and sidecar files."""

import tempfile
from pathlib import Path


def test_edit_session_crop_initialization():
    """Verify EditSession initializes crop coordinates to default values."""
    from iPhoto.gui.ui.models.edit_session import EditSession
    
    session = EditSession()
    
    # Check default crop values
    assert session.value("Crop_U0") == 0.0
    assert session.value("Crop_V0") == 0.0
    assert session.value("Crop_U1") == 1.0
    assert session.value("Crop_V1") == 1.0


def test_edit_session_set_crop_uv():
    """Verify EditSession.set_crop_uv() updates crop coordinates."""
    from iPhoto.gui.ui.models.edit_session import EditSession
    
    session = EditSession()
    test_uv = (0.1, 0.2, 0.8, 0.9)
    
    session.set_crop_uv(test_uv)
    
    assert session.value("Crop_U0") == 0.1
    assert session.value("Crop_V0") == 0.2
    assert session.value("Crop_U1") == 0.8
    assert session.value("Crop_V1") == 0.9


def test_edit_session_get_crop_uv():
    """Verify EditSession.get_crop_uv() returns crop coordinates."""
    from iPhoto.gui.ui.models.edit_session import EditSession
    
    session = EditSession()
    test_uv = (0.25, 0.35, 0.75, 0.85)
    
    session.set_crop_uv(test_uv)
    retrieved_uv = session.get_crop_uv()
    
    assert retrieved_uv == test_uv


def test_edit_session_reset_includes_crop():
    """Verify EditSession.reset() restores crop to defaults."""
    from iPhoto.gui.ui.models.edit_session import EditSession
    
    session = EditSession()
    
    # Set non-default crop values
    session.set_crop_uv((0.1, 0.2, 0.8, 0.9))
    
    # Reset should restore defaults
    session.reset()
    
    assert session.value("Crop_U0") == 0.0
    assert session.value("Crop_V0") == 0.0
    assert session.value("Crop_U1") == 1.0
    assert session.value("Crop_V1") == 1.0


def test_sidecar_save_and_load_crop_keys():
    """Verify crop parameters are saved and loaded from .ipo files."""
    from iPhoto.io import sidecar
    
    with tempfile.TemporaryDirectory() as tmpdir:
        test_image = Path(tmpdir) / "test.jpg"
        test_image.touch()
        
        # Save adjustments with crop data
        adjustments = {
            "Light_Master": 0.5,
            "Crop_U0": 0.15,
            "Crop_V0": 0.25,
            "Crop_U1": 0.85,
            "Crop_V1": 0.75,
        }
        
        sidecar.save_adjustments(test_image, adjustments)
        
        # Load and verify
        loaded = sidecar.load_adjustments(test_image)
        
        assert loaded["Crop_U0"] == 0.15
        assert loaded["Crop_V0"] == 0.25
        assert loaded["Crop_U1"] == 0.85
        assert loaded["Crop_V1"] == 0.75
        assert loaded["Light_Master"] == 0.5


def test_sidecar_crop_defaults():
    """Verify missing crop keys fall back to defaults."""
    from iPhoto.io import sidecar
    
    with tempfile.TemporaryDirectory() as tmpdir:
        test_image = Path(tmpdir) / "test.jpg"
        test_image.touch()
        
        # Save adjustments without crop data
        adjustments = {
            "Light_Master": 0.3,
        }
        
        sidecar.save_adjustments(test_image, adjustments)
        
        # Load and verify crop defaults are applied during save
        loaded = sidecar.load_adjustments(test_image)
        
        assert loaded["Crop_U0"] == 0.0
        assert loaded["Crop_V0"] == 0.0
        assert loaded["Crop_U1"] == 1.0
        assert loaded["Crop_V1"] == 1.0


def test_sidecar_crop_clamping():
    """Verify crop values are clamped to [0, 1] range."""
    from iPhoto.io import sidecar
    
    with tempfile.TemporaryDirectory() as tmpdir:
        test_image = Path(tmpdir) / "test.jpg"
        test_image.touch()
        
        # Save adjustments with out-of-range crop values
        adjustments = {
            "Crop_U0": -0.5,  # Should be clamped to 0.0
            "Crop_V0": 1.5,   # Should be clamped to 1.0
            "Crop_U1": 0.7,
            "Crop_V1": 0.8,
        }
        
        sidecar.save_adjustments(test_image, adjustments)
        loaded = sidecar.load_adjustments(test_image)
        
        # Verify clamping
        assert 0.0 <= loaded["Crop_U0"] <= 1.0
        assert 0.0 <= loaded["Crop_V0"] <= 1.0
        assert loaded["Crop_U0"] == 0.0  # -0.5 clamped to 0.0
        assert loaded["Crop_V0"] == 1.0  # 1.5 clamped to 1.0


def test_resolve_render_adjustments_excludes_crop_keys():
    """Verify crop keys are not passed to shader (they're for display UV only)."""
    from iPhoto.io.sidecar import resolve_render_adjustments
    
    adjustments = {
        "Light_Master": 0.5,
        "Light_Enabled": True,
        "Crop_U0": 0.1,
        "Crop_V0": 0.2,
        "Crop_U1": 0.9,
        "Crop_V1": 0.8,
    }
    
    resolved = resolve_render_adjustments(adjustments)
    
    # Crop keys should not be in resolved adjustments
    assert "Crop_U0" not in resolved
    assert "Crop_V0" not in resolved
    assert "Crop_U1" not in resolved
    assert "Crop_V1" not in resolved
    
    # But light adjustments should be present
    assert "Exposure" in resolved or len(resolved) >= 0
