"""Service that owns the asynchronous import workflow for the GUI."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from PySide6.QtCore import QObject, Signal

from ..background_task_manager import BackgroundTaskManager
from ..ui.tasks.import_worker import ImportSignals, ImportWorker
from .album_metadata_service import AlbumMetadataService



class AssetImportService(QObject):
    """Coordinate file imports and surface lifecycle events to the UI."""

    importStarted = Signal(Path)
    importProgress = Signal(Path, int, int)
    importFinished = Signal(Path, bool, str)
    errorRaised = Signal(str)

    def __init__(
        self,
        *,
        task_manager: BackgroundTaskManager,
        current_album_root: Callable[[], Optional[Path]],
        refresh_callback: Callable[[Path], None],
        metadata_service: AlbumMetadataService,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._task_manager = task_manager
        self._current_album_root = current_album_root
        self._refresh_callback = refresh_callback
        self._metadata_service = metadata_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def import_files(
        self,
        sources: Iterable[Path],
        *,
        destination: Optional[Path] = None,
        mark_featured: bool = False,
    ) -> None:
        """Normalise *sources* and import them into the selected destination."""

        normalized = self._normalise_sources(sources)
        if not normalized:
            target_root = self._resolve_import_destination(destination)
            if target_root is not None:
                self.importFinished.emit(target_root, False, "No files were imported.")
            return

        target_root = self._resolve_import_destination(destination)
        if target_root is None:
            return

        signals = ImportSignals()
        signals.started.connect(self.importStarted.emit)
        signals.progress.connect(self.importProgress.emit)

        worker = ImportWorker(normalized, target_root, self._copy_into_album, signals)
        self._task_manager.submit_task(
            task_id=f"import:{target_root}",
            worker=worker,
            started=signals.started,
            progress=signals.progress,
            finished=signals.finished,
            error=signals.error,
            pause_watcher=True,
            on_finished=lambda root, imported, rescan_ok: self._handle_import_finished(
                root,
                imported,
                rescan_ok,
                mark_featured,
            ),
            on_error=self.errorRaised.emit,
            result_payload=lambda root, imported, rescan_ok: imported,
        )

    # ------------------------------------------------------------------
    # Helpers used by the worker lifecycle
    # ------------------------------------------------------------------
    def _normalise_sources(self, sources: Iterable[Path]) -> List[Path]:
        """Return a deduplicated list of input files suitable for importing."""

        normalized: List[Path] = []
        seen: set[Path] = set()
        for candidate in sources:
            try:
                expanded = Path(candidate).expanduser()
            except TypeError:
                continue
            try:
                resolved = expanded.resolve()
            except OSError:
                resolved = expanded
            if resolved in seen:
                continue
            if not resolved.exists() or not resolved.is_file():
                continue
            seen.add(resolved)
            normalized.append(resolved)
        return normalized

    def _resolve_import_destination(self, destination: Optional[Path]) -> Optional[Path]:
        """Return the absolute album root that should receive imported files."""

        if destination is not None:
            try:
                target = Path(destination).expanduser().resolve()
            except OSError as exc:
                self.errorRaised.emit(f"Import destination is not accessible: {exc}")
                return None
        else:
            target = self._current_album_root()
            if target is None:
                self.errorRaised.emit("No album is currently open.")
                return None

        if not target.exists() or not target.is_dir():
            self.errorRaised.emit(f"Import destination is not a directory: {target}")
            return None
        return target

    def _copy_into_album(self, source: Path, destination: Path) -> Path:
        """Copy *source* into *destination* using collision-safe filenames."""

        base_name = source.name
        target = destination / base_name
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = destination / f"{stem} ({counter}){suffix}"
            counter += 1
        destination.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy2(source, target)).resolve()

    def _handle_import_finished(
        self,
        root: Path,
        imported: Sequence[Path],
        rescan_succeeded: bool,
        mark_featured: bool,
    ) -> None:
        """Finalise the import workflow once the worker reports completion."""

        imported_paths = [Path(path) for path in imported]
        success = bool(imported_paths) and rescan_succeeded

        if mark_featured and imported_paths:
            self._metadata_service.ensure_featured_entries(root, imported_paths)

        if rescan_succeeded and imported_paths:
            self._refresh_callback(root)

        if imported_paths:
            label = "file" if len(imported_paths) == 1 else "files"
            if rescan_succeeded:
                message = f"Imported {len(imported_paths)} {label}."
            else:
                message = (
                    f"Imported {len(imported_paths)} {label}, but refreshing the album failed."
                )
        else:
            message = "No files were imported."

        self.importFinished.emit(root, success, message)


__all__ = ["AssetImportService"]
