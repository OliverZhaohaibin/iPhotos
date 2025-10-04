"""Lightweight floating window that previews a video asset."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from ....config import (
    PREVIEW_WINDOW_CLOSE_DELAY_MS,
    PREVIEW_WINDOW_DEFAULT_WIDTH,
    PREVIEW_WINDOW_MUTED,
)
from ..media import MediaController, require_multimedia

if importlib.util.find_spec("PySide6.QtMultimediaWidgets") is not None:
    from PySide6.QtMultimediaWidgets import QVideoWidget
else:  # pragma: no cover - requires optional Qt module
    class QVideoWidget(QWidget):  # type: ignore[misc]
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - fallback
            raise RuntimeError(
                "PySide6.QtMultimediaWidgets is unavailable. Install PySide6 with "
                "QtMultimedia support to preview videos."
            )


class PreviewWindow(QWidget):
    """Frameless preview surface that reuses the media controller API."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        require_multimedia()
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._video_widget = QVideoWidget(self)
        self._video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._video_widget.setStyleSheet("background-color: black;")
        layout.addWidget(self._video_widget)

        self._media = MediaController(self)
        self._media.set_video_output(self._video_widget)
        self._media.set_muted(PREVIEW_WINDOW_MUTED)

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._do_close)
        self.hide()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def show_preview(self, source: Path | str, at: Optional[QRect | QPoint] = None) -> None:
        """Display *source* near *at* and start playback immediately."""

        path = Path(source)
        self._close_timer.stop()
        self._media.stop()
        self._media.load(path)

        width = PREVIEW_WINDOW_DEFAULT_WIDTH
        height = max(1, int(width * 9 / 16))
        self.resize(width, height)

        if at is not None:
            if isinstance(at, QRect):
                origin = at.topRight() + QPoint(16, 0)
            else:
                origin = at
            self.move(origin)

        self.show()
        self.raise_()
        self._media.play()

    def close_preview(self, delayed: bool = True) -> None:
        """Hide the preview window, optionally with a delay."""

        if delayed:
            self._close_timer.start(PREVIEW_WINDOW_CLOSE_DELAY_MS)
        else:
            self._do_close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _do_close(self) -> None:
        self._close_timer.stop()
        self._media.stop()
        self.hide()
