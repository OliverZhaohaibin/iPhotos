from pathlib import Path

from iPhoto.media_classifier import classify_media, VIDEO_EXTENSIONS


def test_classify_media_prefers_mime_for_video() -> None:
    row = {"mime": "video/mp4"}
    assert classify_media(row) == (False, True)


def test_classify_media_handles_uppercase_extension() -> None:
    row = {"rel": "CLIP_0001.MP4"}
    assert classify_media(row) == (False, True)


def test_classify_media_supports_additional_video_extensions() -> None:
    for extension in {".avi", ".wmv", ".mkv"}:
        row = {"rel": f"clip{extension}"}
        assert classify_media(row) == (False, True)
        assert extension in VIDEO_EXTENSIONS


def test_classify_media_identifies_images() -> None:
    row = {"mime": "image/jpeg", "rel": Path("photo.jpg")}
    assert classify_media(row) == (True, False)
