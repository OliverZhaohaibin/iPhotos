"""Worker that executes preview renders on a background thread."""

from __future__ import annotations

from typing import Mapping

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage

from ....core.preview_backends import PreviewBackend, PreviewSession


class PreviewRenderSignals(QObject):
    """Signals emitted by :class:`PreviewRenderWorker`."""

    finished = Signal(QImage, int)
    """Emitted with the rendered frame and the job identifier."""


class PreviewRenderWorker(QRunnable):
    """Apply tone-mapping adjustments using ``PreviewBackend.render``."""

    def __init__(
        self,
        backend: PreviewBackend,
        session: PreviewSession,
        adjustments: Mapping[str, float],
        job_id: int,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._session = session
        self._adjustments = dict(adjustments)
        self._job_id = job_id
        self.signals = PreviewRenderSignals()

    @property
    def session(self) -> PreviewSession:
        """Expose the session so callers can manage resource lifetimes."""

        return self._session

    def run(self) -> None:  # type: ignore[override]
        """Render the adjusted frame and notify listeners when done."""

        try:
            image = self._backend.render(self._session, self._adjustments)
        except Exception:  # pragma: no cover - safety net mirrors legacy guard
            image = QImage()
        self.signals.finished.emit(image, self._job_id)


__all__ = ["PreviewRenderSignals", "PreviewRenderWorker"]
