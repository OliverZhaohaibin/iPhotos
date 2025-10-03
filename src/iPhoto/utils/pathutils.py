"""Utilities for working with filesystem paths inside iPhoto."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Iterable, Iterator


def _expand(pattern: str) -> Iterator[str]:
    match = re.search(r"\{([^}]+)\}", pattern)
    if not match:
        yield pattern
        return
    prefix = pattern[: match.start()]
    suffix = pattern[match.end() :]
    for option in match.group(1).split(","):
        yield from _expand(prefix + option + suffix)


def is_excluded(path: Path, globs: Iterable[str], *, root: Path) -> bool:
    """Return ``True`` if *path* should be excluded based on *globs*.

    The function works on relative POSIX-style paths to provide consistent
    behaviour across operating systems.
    """

    rel = path.relative_to(root).as_posix()
    for pattern in globs:
        for expanded in _expand(pattern):
            if fnmatch.fnmatch(rel, expanded):
                return True
            if expanded.startswith("**/") and fnmatch.fnmatch(rel, expanded[3:]):
                return True
    return False


def should_include(path: Path, include_globs: Iterable[str], exclude_globs: Iterable[str], *, root: Path) -> bool:
    """Return ``True`` if *path* should be scanned."""

    if is_excluded(path, exclude_globs, root=root):
        return False
    rel = path.relative_to(root).as_posix()
    for pattern in include_globs:
        for expanded in _expand(pattern):
            if fnmatch.fnmatch(rel, expanded):
                return True
            if expanded.startswith("**/") and fnmatch.fnmatch(rel, expanded[3:]):
                return True
    return False


def ensure_work_dir(root: Path, name: str = ".iPhoto") -> Path:
    """Ensure that the album work directory exists and return it."""

    work_dir = root / name
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir
