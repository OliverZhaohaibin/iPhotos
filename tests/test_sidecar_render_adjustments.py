from iPhotos.src.iPhoto.core.light_resolver import resolve_light_vector
from iPhotos.src.iPhoto.io.sidecar import resolve_render_adjustments


def test_resolve_render_adjustments_blends_master_and_overrides() -> None:
    raw = {
        "Light_Master": 0.25,
        "Light_Enabled": True,
        "Shadows": 0.15,
    }
    resolved = resolve_render_adjustments(raw)
    expected = resolve_light_vector(0.25, {"Shadows": 0.15})
    assert resolved == expected


def test_resolve_render_adjustments_skips_when_disabled() -> None:
    raw = {
        "Light_Master": 0.6,
        "Light_Enabled": False,
        "Exposure": 0.4,
    }
    resolved = resolve_render_adjustments(raw)
    assert resolved == {}


def test_resolve_render_adjustments_handles_missing_values() -> None:
    resolved = resolve_render_adjustments({})
    assert resolved == {}
