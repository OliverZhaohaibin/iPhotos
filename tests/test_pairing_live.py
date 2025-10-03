from __future__ import annotations

from datetime import datetime, timezone

from iPhotos.src.iPhoto.core.pairing import pair_live


def iso(ts: datetime) -> str:
    return ts.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def test_pairing_prefers_content_id() -> None:
    dt = iso(datetime(2024, 1, 1, 12, 0, 0))
    rows = [
        {
            "rel": "IMG_0001.HEIC",
            "mime": "image/heic",
            "dt": dt,
            "content_id": "CID1",
        },
        {
            "rel": "IMG_0001.MOV",
            "mime": "video/quicktime",
            "dt": dt,
            "content_id": "CID1",
            "dur": 1.5,
            "still_image_time": 0.1,
        },
    ]
    groups = pair_live(rows)
    assert len(groups) == 1
    group = groups[0]
    assert group.still == "IMG_0001.HEIC"
    assert group.motion == "IMG_0001.MOV"
    assert group.content_id == "CID1"
