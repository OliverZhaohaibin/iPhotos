"""Helpers around :class:`QMediaPlayer` for the desktop UI."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QUrl, Signal

if importlib.util.find_spec("PySide6.QtMultimedia") is not None:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
else:  # pragma: no cover - requires optional Qt module
    QAudioOutput = QMediaPlayer = None  # type: ignore[assignment]

from iPhotos.src.iPhoto.config import VIDEO_MEMORY_CACHE_MAX_BYTES


def is_multimedia_available() -> bool:
    """Return whether QtMultimedia is importable in the current environment."""

    return QMediaPlayer is not None and QAudioOutput is not None


def require_multimedia() -> None:
    """Raise a clear error if QtMultimedia support is missing."""

    if not is_multimedia_available():
        raise RuntimeError(
            "PySide6 QtMultimedia modules are unavailable. Install PySide6 with "
            "QtMultimedia support to enable video playback features."
        )


class MediaController(QObject):
    """Thin wrapper that exposes convenience APIs around ``QMediaPlayer``."""

    errorOccurred = Signal(str)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    readyToPlay = Signal()
    mutedChanged = Signal(bool)
    volumeChanged = Signal(int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        require_multimedia()
        assert QMediaPlayer is not None  # for type-checkers
        assert QAudioOutput is not None

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._current_source: Optional[Path] = None
        self._memory_data: Optional[QByteArray] = None
        self._memory_buffer: Optional[QBuffer] = None
        self._has_emitted_ready = False

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self.playbackStateChanged.emit)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._audio.mutedChanged.connect(self.mutedChanged.emit)
        self._audio.volumeChanged.connect(self._on_volume_changed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_video_output(self, widget: object) -> None:
        """Display video frames on *widget*."""

        sink_getter = getattr(widget, "videoSink", None)
        if callable(sink_getter):
            sink = sink_getter()
            if sink is not None:
                self._player.setVideoOutput(sink)
                return

        self._player.setVideoOutput(widget)

    def load(self, path: Path) -> None:
        """Load media from *path* without immediately starting playback."""

        self._current_source = path
        self._release_memory_buffer()
        self._has_emitted_ready = False

        if VIDEO_MEMORY_CACHE_MAX_BYTES > 0:
            try:
                file_size = path.stat().st_size
            except OSError:
                file_size = None
            else:
                if file_size <= VIDEO_MEMORY_CACHE_MAX_BYTES:
                    try:
                        payload = path.read_bytes()
                    except (OSError, MemoryError):
                        payload = None
                    else:
                        buffer_data = QByteArray(payload)
                        buffer = QBuffer(self)
                        buffer.setData(buffer_data)
                        if buffer.open(QIODevice.ReadOnly):
                            self._memory_data = buffer_data
                            self._memory_buffer = buffer
                            self._player.setSourceDevice(
                                buffer, QUrl.fromLocalFile(str(path))
                            )
                            return

        self._player.setSource(QUrl.fromLocalFile(str(path)))

    def play(self) -> None:
        """Start playback of the current media."""

        self._player.play()

    def pause(self) -> None:
        """Pause playback."""

        self._player.pause()

    def stop(self) -> None:
        """Stop playback and reset the playback position to the beginning."""

        self._player.stop()

    def toggle(self) -> None:
        """Toggle between play and pause states."""

        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def seek(self, position_ms: int) -> None:
        """Seek to *position_ms* in the current media."""

        self._player.setPosition(max(0, position_ms))

    def set_volume(self, volume: int) -> None:
        """Set the audio volume to *volume* (0-100)."""

        clamped = max(0, min(100, volume))
        self._audio.setVolume(clamped / 100.0)

    def volume(self) -> int:
        """Return the current volume in the 0-100 range."""

        return int(round(self._audio.volume() * 100))

    def set_muted(self, muted: bool) -> None:
        """Mute or unmute audio output."""

        self._audio.setMuted(muted)

    def is_muted(self) -> bool:
        """Return whether the audio output is currently muted."""

        return self._audio.isMuted()

    def playback_state(self) -> object:
        """Return the current playback state."""

        return self._player.playbackState()

    def current_source(self) -> Optional[Path]:
        """Return the currently loaded media source, if any."""

        return self._current_source

    def video_sink(self) -> Optional[object]:
        """Return the underlying video sink used for rendering, if available."""

        getter = getattr(self._player, "videoSink", None)
        if callable(getter):
            sink = getter()
            if sink is not None:
                return sink
        return None

    # ------------------------------------------------------------------
    # Slots for internal signal forwarding
    # ------------------------------------------------------------------
    def _on_position_changed(self, position: int) -> None:
        self.positionChanged.emit(int(position))

    def _on_duration_changed(self, duration: int) -> None:
        self.durationChanged.emit(int(duration))

    def _on_volume_changed(self, volume: float) -> None:
        self.volumeChanged.emit(int(round(volume * 100)))

    def _on_media_status_changed(self, status: object) -> None:
        self.mediaStatusChanged.emit(status)
        if self._has_emitted_ready:
            return
        status_name = getattr(status, "name", None)
        ready_names = {"LoadedMedia", "BufferedMedia", "BufferingMedia"}
        if status_name in ready_names:
            self._has_emitted_ready = True
            self.readyToPlay.emit()
            return
        if QMediaPlayer is not None:
            try:
                media_status = QMediaPlayer.MediaStatus(status)  # type: ignore[arg-type]
            except Exception:
                return
            if media_status in {
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
                QMediaPlayer.MediaStatus.BufferingMedia,
            }:
                self._has_emitted_ready = True
                self.readyToPlay.emit()

    def _on_error(self, _error: object, message: str) -> None:
        if message:
            self.errorOccurred.emit(message)
        else:  # pragma: no cover - Qt may provide empty strings
            self.errorOccurred.emit("An unknown media playback error occurred.")

    def _release_memory_buffer(self) -> None:
        """Release any in-memory cached media payload."""

        if self._memory_buffer is not None and self._memory_buffer.isOpen():
            self._memory_buffer.close()
        self._memory_buffer = None
        self._memory_data = None
