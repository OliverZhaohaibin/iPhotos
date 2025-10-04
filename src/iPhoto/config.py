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

THUMBNAIL_SEEK_GUARD_SEC: Final[float] = 0.35

SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parent / "schemas"
ALBUM_MANIFEST_NAMES: Final[list[str]] = [".iphoto.album.json", ".iPhoto/manifest.json"]
WORK_DIR_NAME: Final[str] = ".iPhoto"

# ---------------------------------------------------------------------------
# Album creation helpers
# ---------------------------------------------------------------------------

ALBUM_HOVER_FADE_MS: Final[int] = 200
NEW_ALBUM_ICON_PATH: Final[str] = "gui/ui/icon/plus.circle.svg"
NEW_ALBUM_DEFAULT_NAME: Final[str] = "New Album"

# ---------------------------------------------------------------------------
# UI interaction constants
# ---------------------------------------------------------------------------

LONG_PRESS_THRESHOLD_MS: Final[int] = 350
PREVIEW_WINDOW_DEFAULT_WIDTH: Final[int] = 640
PREVIEW_WINDOW_MUTED: Final[bool] = True
PREVIEW_WINDOW_CLOSE_DELAY_MS: Final[int] = 150

# Maximum number of bytes to preload into memory for the active video. When the
# file on disk is smaller than this threshold the media controller will stream
# it from RAM to make seeking as responsive as possible.
VIDEO_MEMORY_CACHE_MAX_BYTES: Final[int] = 512 * 1024 * 1024

# When a video finishes playing we step backwards by this many milliseconds and
# pause so that the last frame remains visible instead of flashing to black.
VIDEO_COMPLETE_HOLD_BACKSTEP_MS: Final[int] = 80
