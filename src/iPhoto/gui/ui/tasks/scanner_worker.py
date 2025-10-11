"""Background worker that scans albums while reporting progress."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from PySide6.QtCore import QObject, QRunnable, Signal

from ....config import WORK_DIR_NAME
from ....io.scanner import gather_media_paths, process_media_paths
from ....utils.pathutils import ensure_work_dir


class ScannerSignals(QObject):
    """Signals emitted by :class:`ScannerWorker` while scanning."""

    progressUpdated = Signal(Path, int, int)
    finished = Signal(Path, list)
    error = Signal(Path, str)


class ScannerWorker(QRunnable):
    """Scan album files in a worker thread and emit progress updates."""

    def __init__(
        self,
        root: Path,
        include: Iterable[str],
        exclude: Iterable[str],
        signals: ScannerSignals,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._root = root
        self._include = list(include)
        self._exclude = list(exclude)
        self._signals = signals
        self._is_cancelled = False
        self._had_error = False

    @property
    def root(self) -> Path:
        """Album directory being scanned."""

        return self._root

    @property
    def signals(self) -> ScannerSignals:
        """Signal container used by this worker."""

        return self._signals

    @property
    def cancelled(self) -> bool:
        """Return ``True`` if the scan has been cancelled."""

        return self._is_cancelled

    @property
    def failed(self) -> bool:
        """Return ``True`` if the scan terminated due to an error."""

        return self._had_error

    def run(self) -> None:  # pragma: no cover - executed on worker thread
        """Perform the scan and emit progress as files are processed."""

        rows: List[dict] = []
        try:
            ensure_work_dir(self._root, WORK_DIR_NAME)

            # Emit an initial indeterminate update so the UI can show a busy
            # indicator while we enumerate the filesystem.  This mirrors the
            # behaviour of the legacy implementation to keep the UX familiar.
            self._signals.progressUpdated.emit(self._root, 0, -1)

            image_paths, video_paths = gather_media_paths(
                self._root, self._include, self._exclude
            )
            if self._is_cancelled:
                return

            total_files = len(image_paths) + len(video_paths)
            self._signals.progressUpdated.emit(self._root, 0, total_files)
            if total_files == 0:
                self._signals.finished.emit(self._root, [])
                return

            processed_count = 0
            last_reported = 0
            for row in process_media_paths(self._root, image_paths, video_paths):
                if self._is_cancelled:
                    return
                rows.append(row)
                processed_count += 1

                # To avoid overwhelming the UI thread we only emit progress
                # every 25 items (and always on completion).  This matches the
                # cadence of the original worker implementation.
                if processed_count == total_files or processed_count - last_reported >= 25:
                    self._signals.progressUpdated.emit(
                        self._root, processed_count, total_files
                    )
                    last_reported = processed_count
        except Exception as exc:  # pragma: no cover - best-effort error propagation
            if not self._is_cancelled:
                self._had_error = True
                self._signals.error.emit(self._root, str(exc))
        finally:
            if not self._is_cancelled and not self._had_error:
                self._signals.finished.emit(self._root, rows)
            else:
                self._signals.finished.emit(self._root, [])

    def cancel(self) -> None:
        """Request cancellation of the in-progress scan."""

        self._is_cancelled = True
