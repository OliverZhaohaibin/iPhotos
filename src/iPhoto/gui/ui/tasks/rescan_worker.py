"""Background worker that refreshes album indexes after restore operations."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from .... import app as backend
from ....errors import IPhotoError


class RescanSignals(QObject):
    """Signal bundle emitted by :class:`RescanWorker` while executing."""

    finished = Signal(Path, bool)
    error = Signal(Path, str)


class RescanWorker(QRunnable):
    """Execute a blocking ``backend.rescan`` call on a worker thread."""

    def __init__(self, root: Path, signals: RescanSignals) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._root = Path(root)
        self._signals = signals

    @property
    def root(self) -> Path:
        """Return the album directory that will be refreshed."""

        return self._root

    @property
    def signals(self) -> RescanSignals:
        """Expose the signal container so callers can wire it up."""

        return self._signals

    def run(self) -> None:  # pragma: no cover - executed on worker thread
        """Perform the rescan and emit the outcome back to the GUI thread."""

        success = False
        try:
            backend.rescan(self._root)
        except IPhotoError as exc:
            # Surface domain-specific failures with the album path attached so the
            # facade can relay meaningful diagnostics to the user.
            self._signals.error.emit(self._root, str(exc))
        except Exception as exc:  # pragma: no cover - defensive safety net
            self._signals.error.emit(self._root, str(exc))
        else:
            success = True
        finally:
            # Always emit ``finished`` so the task manager can release bookkeeping
            # regardless of success or failure.
            self._signals.finished.emit(self._root, success)


__all__ = ["RescanSignals", "RescanWorker"]

