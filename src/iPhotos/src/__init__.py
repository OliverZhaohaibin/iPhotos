"""Map the legacy ``iPhotos.src`` namespace to the new ``src`` package."""

from __future__ import annotations

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parents[2])]  # type: ignore[var-annotated]
