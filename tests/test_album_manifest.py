from __future__ import annotations

from pathlib import Path

from iPhoto.models.album import Album


def test_open_temp_album(tmp_path: Path) -> None:
    album = Album.open(tmp_path)
    assert album.manifest["title"] == tmp_path.name


def test_save_manifest(tmp_path: Path) -> None:
    album = Album.open(tmp_path)
    album.set_cover("IMG_0001.JPG")
    path = album.save()
    assert path.exists()
    saved = Album.open(tmp_path)
    assert saved.manifest["cover"] == "IMG_0001.JPG"
