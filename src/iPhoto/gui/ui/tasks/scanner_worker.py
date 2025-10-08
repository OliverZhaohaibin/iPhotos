"""Background worker that scans albums while reporting progress."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

from PySide6.QtCore import QObject, Signal

from ....config import WORK_DIR_NAME
from ....io.scanner import _build_row
from ....utils.pathutils import ensure_work_dir, is_excluded, should_include


class ScannerWorker(QObject):
    """Scan album files in a worker thread and emit progress updates."""

    progressUpdated = Signal(int, int)
    finished = Signal(list)
    error = Signal(str)

    def __init__(
        self,
        root: Path,
        include: Iterable[str],
        exclude: Iterable[str],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._root = root
        self._include = list(include)
        self._exclude = list(exclude)
        self._is_cancelled = False

    def run(self) -> None:
        """Perform the scan and emit progress as files are processed."""

        try:
            ensure_work_dir(self._root, WORK_DIR_NAME)
            all_files = [path for path in self._root.rglob("*") if path.is_file()]
            total_files = len(all_files)
            if total_files == 0:
                self.progressUpdated.emit(0, 0)
                if not self._is_cancelled:
                    self.finished.emit([])
                return

            rows: List[dict] = []
            for index, file_path in enumerate(all_files, start=1):
                if self._is_cancelled:
                    break
                row = self._process_single_file(file_path)
                if row is not None:
                    rows.append(row)
                self.progressUpdated.emit(index, total_files)

            if not self._is_cancelled:
                self.finished.emit(rows)
        except Exception as exc:  # pragma: no cover - best-effort error propagation
            self.error.emit(str(exc))

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
