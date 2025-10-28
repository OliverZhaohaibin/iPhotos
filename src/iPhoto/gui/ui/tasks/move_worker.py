"""Worker that moves assets between albums on a background thread."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from PySide6.QtCore import QObject, QRunnable, Signal

from .... import app as backend
from ....errors import IPhotoError
from ....cache.index_store import IndexStore
from ....io.scanner import process_media_paths
from ....media_classifier import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS


class MoveSignals(QObject):
    """Qt signal bundle used by :class:`MoveWorker` to report progress."""

    started = Signal(Path, Path)
    progress = Signal(Path, int, int)
    # NOTE: Qt's meta-object system cannot parse typing information such as ``list[Path]``
    # when compiling the signal signature. Using the bare ``list`` type keeps the
    # signature compatible across PySide6 versions while still conveying that a Python
    # list containing :class:`pathlib.Path` objects will be emitted.
    # ``finished`` now emits the source root, destination root, a list of
    # ``(original, target)`` path tuples, and two booleans indicating whether the
    # on-disk caches were updated successfully for the respective albums.
    finished = Signal(Path, Path, list, bool, bool)
    error = Signal(str)


class MoveWorker(QRunnable):
    """Move media files to a different album and refresh index caches."""

    def __init__(
        self,
        sources: Iterable[Path],
        source_root: Path,
        destination_root: Path,
        signals: MoveSignals,
        *,
        library_root: Optional[Path] = None,
        trash_root: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._sources = [Path(path) for path in sources]
        self._source_root = Path(source_root)
        self._destination_root = Path(destination_root)
        self._signals = signals
        self._cancel_requested = False
        self._library_root = self._resolve_optional(library_root)
        self._trash_root = self._resolve_optional(trash_root)
        self._destination_resolved = self._resolve_optional(self._destination_root)
        self._is_trash_destination = bool(
            self._destination_resolved
            and self._trash_root
            and self._destination_resolved == self._trash_root
        )

    @property
    def signals(self) -> MoveSignals:
        """Expose the signal container to callers."""

        return self._signals

    def cancel(self) -> None:
        """Request cancellation of the move operation."""

        self._cancel_requested = True

    @property
    def cancelled(self) -> bool:
        """Return ``True`` when the worker was asked to stop early."""

        return self._cancel_requested

    def run(self) -> None:  # pragma: no cover - executed on a worker thread
        """Move the queued files while updating progress and rescanning albums."""

        total = len(self._sources)
        self._signals.started.emit(self._source_root, self._destination_root)
        if total == 0:
            self._signals.finished.emit(
                self._source_root,
                self._destination_root,
                [],
                True,
                True,
            )
            return

        moved: List[Tuple[Path, Path]] = []
        for index, source in enumerate(self._sources, start=1):
            if self._cancel_requested:
                break
            try:
                try:
                    source_path = source.resolve()
                except OSError:
                    source_path = source
                target = self._move_into_destination(source_path)
            except FileNotFoundError:
                self._signals.error.emit(f"File not found: {source}")
            except OSError as exc:
                self._signals.error.emit(f"Could not move '{source}': {exc}")
            else:
                moved.append((source_path, target))
            finally:
                self._signals.progress.emit(self._source_root, index, total)

        source_index_ok = True
        destination_index_ok = True
        if moved and not self._cancel_requested:
            try:
                self._update_source_index(moved)
            except IPhotoError as exc:
                source_index_ok = False
                self._signals.error.emit(str(exc))
            try:
                self._update_destination_index(moved)
            except IPhotoError as exc:
                destination_index_ok = False
                self._signals.error.emit(str(exc))

        self._signals.finished.emit(
            self._source_root,
            self._destination_root,
            moved,
            source_index_ok,
            destination_index_ok,
        )

    def _move_into_destination(self, source: Path) -> Path:
        """Move *source* into the destination album avoiding name collisions."""

        if not source.exists():
            raise FileNotFoundError(source)
        target_dir = self._destination_root
        base_name = source.name
        target = target_dir / base_name
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = target_dir / f"{stem} ({counter}){suffix}"
            counter += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        moved_path = shutil.move(str(source), str(target))
        return Path(moved_path).resolve()

    def _update_source_index(self, moved: List[Tuple[Path, Path]]) -> None:
        """Remove moved assets from the source album's index and links."""

        store = IndexStore(self._source_root)
        rels = []
        for original, _ in moved:
            try:
                rels.append(original.resolve().relative_to(self._source_root).as_posix())
            except ValueError:
                continue
        store.remove_rows(rels)
        backend.pair(self._source_root)

    def _update_destination_index(self, moved: List[Tuple[Path, Path]]) -> None:
        """Append moved assets to the destination album's index and links."""

        store = IndexStore(self._destination_root)
        image_paths: List[Path] = []
        video_paths: List[Path] = []
        for _, target in moved:
            suffix = target.suffix.lower()
            if suffix in IMAGE_EXTENSIONS:
                image_paths.append(target)
            elif suffix in VIDEO_EXTENSIONS:
                video_paths.append(target)
            else:
                image_paths.append(target)
        new_rows = list(
            process_media_paths(self._destination_root, image_paths, video_paths)
        )
        if self._is_trash_destination and self._library_root is not None:
            source_lookup: Dict[str, Path] = {}
            for original, target in moved:
                target_key = self._normalised_string(target)
                if target_key:
                    source_lookup[target_key] = original

            annotated_rows: List[Dict[str, object]] = []
            for row in new_rows:
                rel_value = row.get("rel") if isinstance(row, dict) else None
                if not isinstance(rel_value, str):
                    annotated_rows.append(row)
                    continue
                absolute_target = self._destination_root / rel_value
                target_key = self._normalised_string(absolute_target)
                original_path = source_lookup.get(target_key) if target_key else None
                if original_path is None:
                    annotated_rows.append(row)
                    continue
                original_relative = self._library_relative(original_path)
                if original_relative is None:
                    annotated_rows.append(row)
                    continue
                # Persist the original library-relative location so restore operations can
                # return the asset to its previous album.
                enriched = dict(row)
                enriched["original_rel_path"] = original_relative
                annotated_rows.append(enriched)
            new_rows = annotated_rows
        store.append_rows(new_rows)
        backend.pair(self._destination_root)

    def _resolve_optional(self, path: Optional[Path]) -> Optional[Path]:
        """Resolve *path* defensively, returning ``None`` when unavailable."""

        if path is None:
            return None
        try:
            return path.resolve()
        except OSError:
            return path

    def _normalised_string(self, path: Path) -> Optional[str]:
        """Return a stable string identifier for *path* suitable for lookups."""

        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        return str(resolved)

    def _library_relative(self, original_path: Path) -> Optional[str]:
        """Compute the original path relative to the library root when possible."""

        library_root = self._library_root
        if library_root is None:
            return None
        try:
            relative = original_path.resolve().relative_to(library_root)
        except (OSError, ValueError):
            try:
                relative = original_path.relative_to(library_root)
            except ValueError:
                return None
        return relative.as_posix()


__all__ = ["MoveSignals", "MoveWorker"]
