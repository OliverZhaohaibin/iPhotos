"""Utilities for optional third-party dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import textwrap
from typing import Any, Optional


@dataclass(frozen=True)
class PillowSupport:
    """Container exposing Pillow objects when the library is available."""

    Image: Any
    ImageOps: Any
    ImageQt: Any
    UnidentifiedImageError: Any


@lru_cache(maxsize=1)
def load_pillow() -> Optional[PillowSupport]:
    """Return Pillow helpers when the dependency can be imported safely.

    Some Windows Python distributions ship without the optional ``_ctypes``
    extension, which in turn prevents Pillow from importing. Importing Pillow in
    that scenario raises ``ImportError`` with a message similar to ``DLL load
    failed while importing _ctypes``. Importing ``_ctypes`` eagerly allows us to
    detect that situation and gracefully disable Pillow-backed features without
    surfacing the exception to callers.
    """

    try:
        import _ctypes  # type: ignore  # noqa: F401 - only used to test availability
    except ImportError:
        return None

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
        from PIL.ImageQt import ImageQt
    except Exception:  # pragma: no cover - optional dependency missing or broken
        return None

    try:  # pragma: no cover - pillow-heif optional
        from pillow_heif import register_heif_opener
    except Exception:  # pragma: no cover - pillow-heif not installed
        register_heif_opener = None
    else:
        try:
            register_heif_opener()
        except Exception:
            # ``pillow-heif`` is optional; ignore registration failures.
            pass

    return PillowSupport(
        Image=Image,
        ImageOps=ImageOps,
        ImageQt=ImageQt,
        UnidentifiedImageError=UnidentifiedImageError,
    )


@dataclass(frozen=True)
class DebuggerPrerequisites:
    """Result describing whether debugger integrations can load."""

    has_ctypes: bool
    message: Optional[str]


@lru_cache(maxsize=1)
def debugger_prerequisites() -> DebuggerPrerequisites:
    """Check whether CPython's optional ``_ctypes`` module is available.

    PyCharm's debugger relies on the ``ctypes`` module which in turn requires the
    native ``_ctypes`` extension. Some Windows-focused Python builds (notably a
    subset of Anaconda distributions) omit that binary, leading to an import
    failure when launching the debugger. Returning a structured result allows the
    CLI, GUI, or helper scripts to surface actionable guidance without crashing.
    """

    try:
        import ctypes  # noqa: F401 - import used to assert availability
    except ImportError as exc:  # pragma: no cover - exercised in Windows builds
        guidance = textwrap.dedent(
            """
            Python failed to import the built-in ``_ctypes`` extension. PyCharm's
            debugger depends on that module, so debugging cannot start until the
            runtime provides it. On Anaconda-based environments, installing the
            optional ``libffi`` package and reinstalling Python usually resolves
            the issue:

                conda install libffi
                conda install python --force-reinstall

            Alternatively, switch to an official CPython build from python.org,
            which always bundles ``_ctypes``.
            """
        ).strip()
        return DebuggerPrerequisites(False, f"{guidance}\n\nOriginal error: {exc}")

    return DebuggerPrerequisites(True, None)
