"""Expose the actual source tree under the ``iPhotos.src`` namespace."""

from __future__ import annotations

from pathlib import Path

# Re-export modules from the real ``src`` directory so ``iPhotos.src.iPhoto`` works.
__path__ = [str(Path(__file__).resolve().parents[2] / "src")]  # type: ignore[var-annotated]
