"""Background worker that computes slider thumbnails off the GUI thread."""

from __future__ import annotations

import logging
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtGui import QImage


LOGGER = logging.getLogger(__name__)


class ThumbnailGeneratorSignals(QObject):
    """Signals emitted by :class:`ThumbnailGeneratorWorker`."""

    thumbnail_ready = Signal(int, QImage, int)
    """Emitted when a single thumbnail preview has been generated."""

    finished = Signal(int)
    """Emitted after every preview has been processed."""

    error = Signal(int, str)
    """Emitted if an unexpected exception aborts the worker."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)


class ThumbnailGeneratorWorker(QRunnable):
    """Generate adjustment previews in a :class:`QThreadPool` worker."""

    def __init__(
        self,
        source_image: QImage,
        values: Sequence[float],
        generator: Callable[[QImage, float], QImage],
        *,
        target_height: int,
        generation_id: int,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)

        # ``QImage`` implements implicit sharing, therefore copying it here keeps the caller's
        # instance detached even when the worker manipulates the data from another thread.
        self._source_image = QImage(source_image)
        self._values = list(values)
        self._generator = generator
        self._target_height = max(1, int(target_height))
        self._generation_id = generation_id
        self.signals = ThumbnailGeneratorSignals()

    # ------------------------------------------------------------------
    def run(self) -> None:  # type: ignore[override]
        """Execute the thumbnail generation loop on a background thread."""

        if not self._values:
            self.signals.finished.emit(self._generation_id)
            return

        if self._source_image.isNull():
            self.signals.finished.emit(self._generation_id)
            return

        try:
            base = self._source_image.scaledToHeight(
                self._target_height,
                Qt.TransformationMode.SmoothTransformation,
            )
            if base.isNull():
                base = QImage(self._source_image)

            # Convert once so the worker always feeds a predictable pixel format into the
            # preview generator, regardless of the source image's colour space.
            base = base.convertToFormat(QImage.Format.Format_ARGB32)

            for index, value in enumerate(self._values):
                # Provide a detached copy so filter routines are free to mutate their input.
                preview_source = QImage(base)
                result = self._generator(preview_source, float(value))
                if result.isNull():
                    continue
                converted = result.convertToFormat(QImage.Format.Format_ARGB32)
                self.signals.thumbnail_ready.emit(index, converted, self._generation_id)
        except Exception as exc:  # pragma: no cover - defensive logging path
            LOGGER.exception("Thumbnail generation failed")
            self.signals.error.emit(self._generation_id, str(exc))
        finally:
            self.signals.finished.emit(self._generation_id)


__all__ = ["ThumbnailGeneratorSignals", "ThumbnailGeneratorWorker"]
