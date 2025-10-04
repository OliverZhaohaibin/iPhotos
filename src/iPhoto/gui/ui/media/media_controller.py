"""Helpers around :class:`QMediaPlayer` for the desktop UI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget


class MediaController(QObject):
    """Thin wrapper that exposes convenience APIs around ``QMediaPlayer``."""

    errorOccurred = Signal(str)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(QMediaPlayer.PlaybackState)
    mutedChanged = Signal(bool)
    volumeChanged = Signal(int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._video_widget: Optional[QVideoWidget] = None
        self._current_source: Optional[Path] = None

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self.playbackStateChanged)
        self._player.errorOccurred.connect(self._on_error)
        self._audio.mutedChanged.connect(self.mutedChanged)
        self._audio.volumeChanged.connect(self._on_volume_changed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_video_output(self, widget: QVideoWidget) -> None:
        """Display video frames on *widget*."""

        self._video_widget = widget
        self._player.setVideoOutput(widget)

    def load(self, path: Path) -> None:
        """Load media from *path* without immediately starting playback."""

        self._current_source = path
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

    def playback_state(self) -> QMediaPlayer.PlaybackState:
        """Return the current playback state."""

        return self._player.playbackState()

    def current_source(self) -> Optional[Path]:
        """Return the currently loaded media source, if any."""

        return self._current_source

    # ------------------------------------------------------------------
    # Slots for internal signal forwarding
    # ------------------------------------------------------------------
    def _on_position_changed(self, position: int) -> None:
        self.positionChanged.emit(int(position))

    def _on_duration_changed(self, duration: int) -> None:
        self.durationChanged.emit(int(duration))

    def _on_volume_changed(self, volume: float) -> None:
        self.volumeChanged.emit(int(round(volume * 100)))

    def _on_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        if message:
            self.errorOccurred.emit(message)
        else:  # pragma: no cover - Qt may provide empty strings
            self.errorOccurred.emit("An unknown media playback error occurred.")
