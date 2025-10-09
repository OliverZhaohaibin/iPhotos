"""Background worker that rebuilds Live Photo pairings off the UI thread."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from .... import app as backend
from ....cache.index_store import IndexStore
from ....errors import IPhotoError, IndexCorruptedError


class LivePairingWorker(QObject, QRunnable):
    """Invoke :func:`backend.pair` in a thread pool."""

    finished = Signal(object, bool)
    error = Signal(object, str)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root

    def run(self) -> None:  # pragma: no cover - executed on worker thread
        try:
            store = IndexStore(self._root)
            if not store.path.exists():
                self.finished.emit(self._root, True)
                return
            rows = list(store.read_all())
            backend._ensure_links(self._root, rows)
            self.finished.emit(self._root, True)
        except (IPhotoError, FileNotFoundError, IndexCorruptedError) as exc:
            self.error.emit(self._root, str(exc))
            self.finished.emit(self._root, False)
        except Exception as exc:  # pragma: no cover - surfaced via signal
            self.error.emit(self._root, str(exc))
            self.finished.emit(self._root, False)
