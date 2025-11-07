"""Background worker that prepares preview assets for the edit sidebar."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtGui import QImage

from ....core.color_resolver import ColorStats, compute_color_statistics

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EditSidebarPreviewResult:
    """Container bundling the scaled preview image and sampled colour stats."""

    image: QImage
    stats: ColorStats


class EditSidebarPreviewSignals(QObject):
    """Signals emitted by :class:`EditSidebarPreviewWorker`."""

    ready = Signal(EditSidebarPreviewResult, int)
    """Delivered when the preview image and colour statistics are available."""

    error = Signal(int, str)
    """Emitted if any unexpected exception aborts the worker."""

    finished = Signal(int)
    """Emitted once the worker has completed, even on failure."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)


class EditSidebarPreviewWorker(QRunnable):
    """Scale full-resolution frames for the edit sidebar off the GUI thread."""

    def __init__(
        self,
        source_image: QImage,
        *,
        generation: int,
        target_height: int,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)

        # ``QImage`` implements implicit data sharing, however copying it here keeps the caller's
        # instance detached.  The worker may mutate pixels while filtering, so a deep copy avoids
        # cross-thread races and prevents Qt from throwing warnings about detached buffers.
        self._source_image = QImage(source_image)
        self._generation = int(generation)
        requested_height = int(target_height)
        self._target_height = -1 if requested_height < 0 else max(64, requested_height)
        self.signals = EditSidebarPreviewSignals()

    # ------------------------------------------------------------------
    def run(self) -> None:  # type: ignore[override]
        """Prepare the sidebar preview image and compute colour statistics."""

        if self._source_image.isNull():
            self.signals.error.emit(self._generation, "Sidebar preview source image was empty")
            self.signals.finished.emit(self._generation)
            return

        try:
            if self._target_height == -1:
                preview = self._source_image
                if preview.format() != QImage.Format.Format_ARGB32:
                    preview = preview.convertToFormat(QImage.Format.Format_ARGB32)
            else:
                preview = self._prepare_preview_image(self._source_image)

            stats = self._compute_statistics(preview)
            result = EditSidebarPreviewResult(preview, stats)
            self.signals.ready.emit(result, self._generation)
        except Exception as exc:  # pragma: no cover - defensive logging path
            _LOGGER.exception("Failed to prepare edit sidebar preview")
            self.signals.error.emit(self._generation, str(exc))
        finally:
            self.signals.finished.emit(self._generation)

    # ------------------------------------------------------------------
    def _prepare_preview_image(self, source: QImage) -> QImage:
        """Return a scaled, format-normalised copy of *source* for thumbnails."""

        scaled = source.scaledToHeight(
            self._target_height,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.isNull():
            # ``scaledToHeight`` can return a null image when Qt fails to allocate the buffer.
            # Fall back to the original frame so the sidebar still receives a valid preview.
            scaled = QImage(source)
        return scaled.convertToFormat(QImage.Format.Format_ARGB32)

    def _compute_statistics(self, preview: QImage) -> ColorStats:
        """Compute colour statistics from *preview* for use by the Color sliders."""

        # The colour statistics are derived from the scaled preview.  Sampling the reduced frame
        # keeps the worker fast enough to run during transitions while still capturing the global
        # balance required by the colour adjustment heuristics.
        return compute_color_statistics(preview)


__all__ = [
    "EditSidebarPreviewResult",
    "EditSidebarPreviewSignals",
    "EditSidebarPreviewWorker",
]
