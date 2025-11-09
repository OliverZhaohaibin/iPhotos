"""Logging helpers for iPhoto."""

from __future__ import annotations

import logging
from typing import Optional

_LOGGER: Optional[logging.Logger] = None


def get_logger() -> logging.Logger:
    """Return a module-level logger configured for iPhoto."""

    global _LOGGER
    if _LOGGER is None:
        _LOGGER = logging.getLogger("iPhoto")
        if not _LOGGER.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            handler.setFormatter(formatter)
            _LOGGER.addHandler(handler)
        _LOGGER.setLevel(logging.INFO)
    return _LOGGER

logger = get_logger()