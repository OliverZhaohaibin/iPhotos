"""Worker that computes colour statistics for preview rendering asynchronously."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage

from ....core.color_resolver import ColorStats, compute_color_statistics


class ColorStatsWorkerSignals(QObject):
    """Signals relaying :class:`ColorStatsWorker` completion events."""

    completed = Signal(ColorStats)
    """Emitted when statistics were calculated successfully."""

    failed = Signal(str)
    """Emitted when statistics could not be computed."""


class ColorStatsWorker(QRunnable):
    """Compute colour statistics for a :class:`~PySide6.QtGui.QImage`."""

    def __init__(self, image: QImage) -> None:
        super().__init__()
        # ``QImage`` uses implicit sharing, so ``copy`` ensures the worker
        # operates on a detached buffer even if the caller mutates the original
        # image later on the GUI thread.
        self._image = QImage(image)
        self.signals = ColorStatsWorkerSignals()

    def run(self) -> None:  # type: ignore[override]
        """Perform the CPU heavy statistics computation."""

        try:
            stats = compute_color_statistics(self._image)
        except Exception as exc:  # pragma: no cover - resiliency path only
            self.signals.failed.emit(str(exc))
            return

        self.signals.completed.emit(stats)


__all__ = ["ColorStatsWorker", "ColorStatsWorkerSignals"]

