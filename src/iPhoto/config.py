"""Default configuration values for iPhoto."""

from __future__ import annotations

from pathlib import Path
from typing import Final

DEFAULT_INCLUDE: Final[list[str]] = ["**/*.{HEIC,JPG,JPEG,PNG,MOV,MP4}"]
DEFAULT_EXCLUDE: Final[list[str]] = ["**/.iPhoto/**", "**/.DS_Store", "**/._*"]
PAIR_TIME_DELTA_SEC: Final[float] = 3.0
LIVE_DURATION_PREFERRED: Final[tuple[float, float]] = (1.0, 3.5)
LOCK_EXPIRE_SEC: Final[int] = 30
THUMB_SIZES: Final[list[tuple[int, int]]] = [(256, 256), (512, 512)]

SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parent / "schemas"
ALBUM_MANIFEST_NAMES: Final[list[str]] = [".iphoto.album.json", ".iPhoto/manifest.json"]
WORK_DIR_NAME: Final[str] = ".iPhoto"
