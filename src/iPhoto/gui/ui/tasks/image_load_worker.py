"""Worker that loads edit images off the UI thread."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage

from ...utils import image_loader


class ImageLoadWorkerSignals(QObject):
    """Signals exposed by :class:`ImageLoadWorker`.

    ``ImageLoadWorker`` lives on a global thread pool.  The signal container is
    kept separate from the runnable itself so slots always execute on the GUI
    thread regardless of which worker picked up the job.
    """

    imageLoaded = Signal(Path, QImage)
    """Emitted once the :class:`QImage` for the requested path is ready."""

    loadFailed = Signal(Path, str)
    """Emitted if decoding the image fails for any reason."""


class ImageLoadWorker(QRunnable):
    """Decode a ``QImage`` for the edit view without blocking the UI."""

    def __init__(self, source: Path) -> None:
        super().__init__()
        self._source = source
        self.signals = ImageLoadWorkerSignals()

    @property
    def source(self) -> Path:
        """Return the absolute path this worker will attempt to load."""

        return self._source

    def run(self) -> None:  # type: ignore[override]
        """Execute the file I/O and decoding work on a background thread."""

        try:
            image = image_loader.load_qimage(self._source)
        except Exception as exc:  # pragma: no cover - best effort propagation
            # Propagate the failure to the controller so it can fall back to the
            # detail view gracefully instead of leaving the user staring at a
            # stuck loading indicator.
            self.signals.loadFailed.emit(self._source, str(exc))
            return

        if image is None or image.isNull():
            self.signals.loadFailed.emit(self._source, "Loaded image is null")
            return

        self.signals.imageLoaded.emit(self._source, image)


__all__ = ["ImageLoadWorker", "ImageLoadWorkerSignals"]
