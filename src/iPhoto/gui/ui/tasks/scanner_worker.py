"""Background worker that scans albums while reporting progress."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

from PySide6.QtCore import QObject, QRunnable, Signal

from ....config import WORK_DIR_NAME
from ....io.scanner import _build_row
from ....utils.pathutils import ensure_work_dir, is_excluded, should_include


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
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._root = root
        self._include = list(include)
        self._exclude = list(exclude)
        self.signals: ScannerSignals = ScannerSignals()
        self._is_cancelled = False
        self._had_error = False

    @property
    def root(self) -> Path:
        """Album directory being scanned."""

        return self._root

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

            self.signals.progressUpdated.emit(self._root, 0, -1)
            all_files: List[Path] = []
            for candidate in self._root.rglob("*"):
                if self._is_cancelled:
                    break
                if candidate.is_file():
                    all_files.append(candidate)

            if self._is_cancelled:
                return

            total_files = len(all_files)
            if total_files == 0:
                self.signals.progressUpdated.emit(self._root, 0, 0)
            else:
                self.signals.progressUpdated.emit(self._root, 0, total_files)
                for index, file_path in enumerate(all_files, start=1):
                    if self._is_cancelled:
                        break
                    row = self._process_single_file(file_path)
                    if row is not None:
                        rows.append(row)
                    if index == total_files or index % 50 == 0:
                        self.signals.progressUpdated.emit(self._root, index, total_files)
        except Exception as exc:  # pragma: no cover - best-effort error propagation
            if not self._is_cancelled:
                self._had_error = True
                self.signals.error.emit(self._root, str(exc))
        finally:
            payload = rows if not (self._is_cancelled or self._had_error) else []
            self.signals.finished.emit(self._root, payload)

    def cancel(self) -> None:
        """Request cancellation of the in-progress scan."""

        self._is_cancelled = True

    def _process_single_file(self, file_path: Path) -> Optional[dict]:
        if WORK_DIR_NAME in file_path.parts:
            return None
        if is_excluded(file_path, self._exclude, root=self._root):
            return None
        if not should_include(file_path, self._include, self._exclude, root=self._root):
            return None
        return _build_row(self._root, file_path)
