"""Reusable playback control bar for the main player."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QToolButton,
    QWidget,
)

from PySide6.QtMultimedia import QMediaPlayer


class PlayerBar(QWidget):
    """Present transport controls, a progress slider and volume settings."""

    playPauseRequested = Signal()
    previousRequested = Signal()
    nextRequested = Signal()
    seekRequested = Signal(int)
    volumeChanged = Signal(int)
    muteToggled = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._duration: int = 0
        self._updating_position = False
        self._scrubbing = False

        self._prev_button = QToolButton(self)
        self._prev_button.setText("⏮")
        self._prev_button.setToolTip("Previous video")

        self._play_button = QToolButton(self)
        self._play_button.setText("▶")
        self._play_button.setToolTip("Play/Pause")

        self._next_button = QToolButton(self)
        self._next_button.setText("⏭")
        self._next_button.setToolTip("Next video")

        self._position_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._position_slider.setRange(0, 0)
        self._position_slider.setSingleStep(1000)
        self._position_slider.setPageStep(5000)
        self._position_slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._elapsed_label = QLabel("0:00", self)
        self._elapsed_label.setMinimumWidth(50)
        self._duration_label = QLabel("0:00", self)
        self._duration_label.setMinimumWidth(50)

        self._volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(80)
        self._volume_slider.setFixedWidth(120)
        self._volume_slider.setToolTip("Volume")

        self._mute_button = QToolButton(self)
        self._mute_button.setText("🔇")
        self._mute_button.setCheckable(True)
        self._mute_button.setToolTip("Mute")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._prev_button)
        layout.addWidget(self._play_button)
        layout.addWidget(self._next_button)
        layout.addWidget(self._elapsed_label)
        layout.addWidget(self._position_slider, stretch=1)
        layout.addWidget(self._duration_label)
        layout.addWidget(self._mute_button)
        layout.addWidget(self._volume_slider)

        self._prev_button.clicked.connect(self.previousRequested)
        self._play_button.clicked.connect(self.playPauseRequested)
        self._next_button.clicked.connect(self.nextRequested)
        self._mute_button.toggled.connect(self.muteToggled)
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        self._position_slider.sliderPressed.connect(self._on_slider_pressed)
        self._position_slider.sliderReleased.connect(self._on_slider_released)
        self._position_slider.valueChanged.connect(self._on_slider_value_changed)

    # ------------------------------------------------------------------
    # UI update helpers
    # ------------------------------------------------------------------
    def set_duration(self, duration_ms: int) -> None:
        """Update the displayed total duration."""

        self._duration = max(0, duration_ms)
        self._duration_label.setText(self._format_ms(self._duration))
        with self._block_position_updates():
            self._position_slider.setRange(0, self._duration if self._duration else 0)
        if self._duration == 0:
            self.set_position(0)

    def set_position(self, position_ms: int) -> None:
        """Update the slider and elapsed label to *position_ms*."""

        position = max(0, min(position_ms, self._duration if self._duration else position_ms))
        with self._block_position_updates():
            self._position_slider.setValue(position)
        self._elapsed_label.setText(self._format_ms(position))

    def set_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        """Switch the play button icon based on *state*."""

        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._play_button.setText("⏸")
        else:
            self._play_button.setText("▶")

    def set_volume(self, volume: int) -> None:
        """Synchronise the volume slider without emitting signals."""

        clamped = max(0, min(100, volume))
        was_blocked = self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(clamped)
        self._volume_slider.blockSignals(was_blocked)

    def set_muted(self, muted: bool) -> None:
        """Synchronise the mute toggle without re-emitting signals."""

        was_blocked = self._mute_button.blockSignals(True)
        self._mute_button.setChecked(muted)
        self._mute_button.blockSignals(was_blocked)

    def reset(self) -> None:
        """Restore the bar to an inactive state."""

        self.set_duration(0)
        self.set_position(0)
        self.set_playback_state(QMediaPlayer.PlaybackState.StoppedState)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _on_volume_changed(self, value: int) -> None:
        self.volumeChanged.emit(value)

    def _on_slider_pressed(self) -> None:
        self._scrubbing = True

    def _on_slider_released(self) -> None:
        self._scrubbing = False
        self.seekRequested.emit(self._position_slider.value())

    def _on_slider_value_changed(self, value: int) -> None:
        if self._updating_position:
            return
        self._elapsed_label.setText(self._format_ms(value))
        if not self._scrubbing:
            self.seekRequested.emit(value)

    # ------------------------------------------------------------------
    # Context managers
    # ------------------------------------------------------------------
    def _block_position_updates(self):
        class _Guard:
            def __init__(self, bar: "PlayerBar") -> None:
                self._bar = bar

            def __enter__(self) -> None:
                self._bar._updating_position = True

            def __exit__(self, exc_type, exc, tb) -> None:
                self._bar._updating_position = False

        return _Guard(self)

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------
    @staticmethod
    def _format_ms(ms: int) -> str:
        total_seconds = max(0, ms // 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"
