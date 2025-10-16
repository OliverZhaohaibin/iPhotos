"""Background worker that copies dropped media into an album asynchronously."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, List

from PySide6.QtCore import QObject, QRunnable, Signal

from .... import app as backend
from ....errors import IPhotoError


class ImportSignals(QObject):
    """Qt signal container used by :class:`ImportWorker` to report progress."""

    started = Signal(Path)
    progress = Signal(Path, int, int)
    finished = Signal(Path, list, bool)
    error = Signal(str)


class ImportWorker(QRunnable):
    """Copy media files on a worker thread and rebuild the album index."""

    def __init__(
        self,
        sources: Iterable[Path],
        destination: Path,
        copier: Callable[[Path, Path], Path],
        signals: ImportSignals,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._sources = [Path(path) for path in sources]
        self._destination = Path(destination)
        self._copier = copier
        self._signals = signals
        self._is_cancelled = False

    @property
    def signals(self) -> ImportSignals:
        """Return the signal bundle associated with the worker."""

        return self._signals

    def cancel(self) -> None:
        """Request cancellation of the in-flight import operation."""

        self._is_cancelled = True

    def run(self) -> None:  # pragma: no cover - executed on a worker thread
        """Copy files and rebuild the index while emitting progress updates."""

        total = len(self._sources)
        self._signals.started.emit(self._destination)
        if total == 0:
            self._signals.finished.emit(self._destination, [], False)
            return

        imported: List[Path] = []
        for index, source in enumerate(self._sources, start=1):
            if self._is_cancelled:
                break
            try:
                copied = self._copier(source, self._destination)
            except OSError as exc:
                # Propagate filesystem issues (permissions, disk space, …) to the UI.
                self._signals.error.emit(f"Could not import '{source}': {exc}")
            except Exception as exc:  # pragma: no cover - defensive fallback
                self._signals.error.emit(str(exc))
            else:
                imported.append(copied)
            finally:
                # Report progress even when a file fails so the UI stays responsive.
                self._signals.progress.emit(self._destination, index, total)

        rescan_success = False
        if imported and not self._is_cancelled:
            try:
                backend.rescan(self._destination)
            except IPhotoError as exc:
                self._signals.error.emit(str(exc))
            except Exception as exc:  # pragma: no cover - defensive fallback
                self._signals.error.emit(str(exc))
            else:
                rescan_success = True

        self._signals.finished.emit(self._destination, imported, rescan_success)
