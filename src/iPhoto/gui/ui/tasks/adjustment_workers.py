"""Background workers for loading and saving edit adjustment sidecars."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from PySide6.QtCore import QObject, QRunnable, Signal

from ....io import sidecar


class AdjustmentLoadWorkerSignals(QObject):
    """Signals emitted by :class:`AdjustmentLoadWorker`."""

    loaded = Signal(Path, dict)
    """Emitted when the sidecar load succeeded."""

    failed = Signal(Path, str)
    """Emitted when the sidecar could not be read."""


class AdjustmentLoadWorker(QRunnable):
    """Load adjustment sidecars off the GUI thread."""

    def __init__(self, source: Path) -> None:
        super().__init__()
        self._source = source
        self.signals = AdjustmentLoadWorkerSignals()

    def run(self) -> None:  # type: ignore[override]
        """Perform the blocking disk I/O on a worker thread."""

        try:
            adjustments = sidecar.load_adjustments(self._source)
        except Exception as exc:  # pragma: no cover - filesystem failures are rare
            # Propagate the failure back to the controller so the edit workflow
            # can fall back to default adjustments without stalling the UI.
            self.signals.failed.emit(self._source, str(exc))
            return

        self.signals.loaded.emit(self._source, dict(adjustments))


class AdjustmentSaveWorkerSignals(QObject):
    """Signals emitted by :class:`AdjustmentSaveWorker`."""

    succeeded = Signal(Path)
    """Emitted once the adjustments were persisted to disk."""

    failed = Signal(Path, str)
    """Emitted when writing the sidecar file failed."""


class AdjustmentSaveWorker(QRunnable):
    """Persist adjustment mappings without blocking the GUI thread."""

    def __init__(self, source: Path, adjustments: Mapping[str, float | bool]) -> None:
        super().__init__()
        self._source = source
        self._adjustments = dict(adjustments)
        self.signals = AdjustmentSaveWorkerSignals()

    def run(self) -> None:  # type: ignore[override]
        """Write the sidecar file in a background thread."""

        try:
            sidecar.save_adjustments(self._source, self._adjustments)
        except Exception as exc:  # pragma: no cover - error propagation only
            self.signals.failed.emit(self._source, str(exc))
            return

        self.signals.succeeded.emit(self._source)


__all__ = [
    "AdjustmentLoadWorker",
    "AdjustmentLoadWorkerSignals",
    "AdjustmentSaveWorker",
    "AdjustmentSaveWorkerSignals",
]

