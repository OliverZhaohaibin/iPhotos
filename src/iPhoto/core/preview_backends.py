"""Hardware aware preview backends for the edit pipeline."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from PySide6.QtGui import QImage

from .image_filters import apply_adjustments

_LOGGER = logging.getLogger(__name__)


class PreviewSession(ABC):
    """Represents a backend specific rendering context.

    Sub-classes encapsulate any state that needs to live between individual
    preview renders.  For the CPU fallback this simply wraps the immutable base
    image, while hardware accelerated variants could retain GPU textures or
    frame-buffer identifiers.  The interface is intentionally tiny so each
    backend can expose only what it needs without leaking implementation
    details into the controller layer.
    """

    @abstractmethod
    def dispose(self) -> None:
        """Release resources associated with the session."""


class PreviewBackend(ABC):
    """Abstract preview backend selecting the optimal rendering strategy."""

    tier_name: str = "unknown"
    """Human readable tier label (e.g. ``"CUDA"`` or ``"CPU"``)."""

    supports_realtime: bool = False
    """Whether the backend can render fast enough to run on the UI thread."""

    @abstractmethod
    def create_session(self, image: QImage) -> PreviewSession:
        """Create a rendering session for *image*.

        Each backend is free to convert the image into whatever representation it
        requires.  The controller keeps the returned session alive for as long as
        the asset remains in the edit view.
        """

    @abstractmethod
    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:
        """Apply *adjustments* and return the preview image."""

    def dispose_session(self, session: PreviewSession) -> None:
        """Release resources owned by *session*.

        Backends override this hook when they allocate handles that must be
        explicitly freed.  The default implementation delegates to the session so
        simple wrappers like the CPU fallback do not need any custom logic.
        """

        session.dispose()


@dataclass
class _CpuPreviewSession(PreviewSession):
    """Store the original image for the CPU fallback backend."""

    image: QImage

    def dispose(self) -> None:  # pragma: no cover - nothing to free
        """Release held resources (no-op for pure CPU sessions)."""

        # No explicit resource management is required for the CPU fallback.  The
        # controller simply drops the reference to the session, allowing Python's
        # garbage collector to reclaim the implicit ``QImage`` copy naturally.
        return


class _CpuPreviewBackend(PreviewBackend):
    """CPU implementation using the existing tone-mapping helpers."""

    tier_name = "CPU"
    supports_realtime = False

    def create_session(self, image: QImage) -> PreviewSession:
        return _CpuPreviewSession(image)

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:
        assert isinstance(session, _CpuPreviewSession)
        return apply_adjustments(session.image, adjustments)


class _CudaPreviewBackend(PreviewBackend):
    """Placeholder for a future CUDA accelerated implementation."""

    tier_name = "CUDA"
    supports_realtime = True

    def __init__(self) -> None:
        raise RuntimeError("CUDA backend is not implemented in this build")

    @classmethod
    def is_available(cls) -> bool:
        """Return ``True`` when the runtime provides the required CUDA stack."""

        try:
            import cupy  # type: ignore  # noqa: F401
        except ImportError:
            return False
        _LOGGER.info("CUDA runtime detected but backend is not yet implemented; skipping")
        return False

    def create_session(self, image: QImage) -> PreviewSession:  # pragma: no cover - not reachable
        raise NotImplementedError

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:  # pragma: no cover - not reachable
        raise NotImplementedError


class _OpenGlPreviewBackend(PreviewBackend):
    """Placeholder for a future OpenGL accelerated implementation."""

    tier_name = "OpenGL"
    supports_realtime = True

    @classmethod
    def is_available(cls) -> bool:
        """Return ``True`` if an OpenGL rendering context is available."""

        try:
            from PySide6 import QtOpenGLWidgets  # type: ignore  # noqa: F401
        except Exception:
            return False
        _LOGGER.info("OpenGL context support detected but backend is not yet implemented; skipping")
        return False

    def create_session(self, image: QImage) -> PreviewSession:  # pragma: no cover - not reachable
        raise NotImplementedError

    def render(self, session: PreviewSession, adjustments: Mapping[str, float]) -> QImage:  # pragma: no cover - not reachable
        raise NotImplementedError


def select_preview_backend() -> PreviewBackend:
    """Return the most capable preview backend available on the system."""

    # CUDA backend has the highest priority.
    if _CudaPreviewBackend.is_available():
        try:
            backend = _CudaPreviewBackend()
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Failed to initialise CUDA backend: %s", exc)
        else:
            _LOGGER.info("Using CUDA preview backend")
            return backend

    # OpenGL is the next best choice when CUDA is not available.
    if _OpenGlPreviewBackend.is_available():
        try:
            backend = _OpenGlPreviewBackend()
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Failed to initialise OpenGL backend: %s", exc)
        else:
            _LOGGER.info("Using OpenGL preview backend")
            return backend

    backend = _CpuPreviewBackend()
    _LOGGER.info("Falling back to CPU preview backend")
    return backend


__all__ = [
    "PreviewBackend",
    "PreviewSession",
    "select_preview_backend",
]
