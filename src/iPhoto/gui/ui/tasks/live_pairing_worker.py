"""Background worker that rebuilds Live Photo pairings off the UI thread."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from .... import app as backend
from ....errors import IPhotoError


class LivePairingWorker(QObject, QRunnable):
    """Invoke :func:`backend.pair` in a thread pool."""

    finished = Signal(object, bool)
    error = Signal(object, str)

    def __init__(self, root: Path) -> None:
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.setAutoDelete(False)
        self._root = root

    def run(self) -> None:  # pragma: no cover - executed on worker thread
        try:
            backend.pair(self._root)
            self.finished.emit(self._root, True)
        except (IPhotoError, FileNotFoundError) as exc:
            self.error.emit(self._root, str(exc))
            self.finished.emit(self._root, False)
        except Exception as exc:  # pragma: no cover - surfaced via signal
            self.error.emit(self._root, str(exc))
            self.finished.emit(self._root, False)
