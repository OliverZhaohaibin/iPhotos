"""Reusable playback control bar for the main player."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


SCRUB_REFRESH_INTERVAL_MS = 33


class PlayerBar(QWidget):
    """Present transport controls, a progress slider and volume settings."""

    playPauseRequested = Signal()
    previousRequested = Signal()
    nextRequested = Signal()
    seekRequested = Signal(int)
    scrubStarted = Signal()
    scrubFinished = Signal()
    volumeChanged = Signal(int)
    muteToggled = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._duration: int = 0
        self._updating_position = False
        self._scrubbing = False

        self._prev_button = self._create_tool_button("⏮", "Previous video")
        self._play_button = self._create_tool_button("▶", "Play/Pause")
        self._play_button.setCheckable(False)
        self._next_button = self._create_tool_button("⏭", "Next video")

        self._scrub_pending_value: Optional[int] = None
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setSingleShot(True)
        self._scrub_timer.timeout.connect(self._emit_pending_scrub)

        self._position_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._position_slider.setRange(0, 0)
        self._position_slider.setSingleStep(1000)
        self._position_slider.setPageStep(5000)
        self._position_slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._elapsed_label = QLabel("0:00", self)
        self._elapsed_label.setMinimumWidth(48)
        self._duration_label = QLabel("0:00", self)
        self._duration_label.setMinimumWidth(48)

        self._volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(80)
        self._volume_slider.setFixedWidth(110)
        self._volume_slider.setToolTip("Volume")

        self._mute_button = self._create_tool_button("🔇", "Mute", checkable=True)

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        slider_row = QHBoxLayout()
        slider_row.setContentsMargins(0, 0, 0, 0)
        slider_row.setSpacing(8)
        slider_row.addWidget(self._elapsed_label)
        slider_row.addWidget(self._position_slider, stretch=1)
        slider_row.addWidget(self._duration_label)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(12)
        controls_row.addStretch(1)
        controls_row.addWidget(self._prev_button)
        controls_row.addWidget(self._play_button)
        controls_row.addWidget(self._next_button)
        controls_row.addStretch(1)
        controls_row.addWidget(self._mute_button)
        controls_row.addWidget(self._volume_slider)
        controls_row.addStretch(1)

        layout.addLayout(slider_row)
        layout.addLayout(controls_row)

        self._apply_palette()

        self._prev_button.clicked.connect(self.previousRequested.emit)
        self._play_button.clicked.connect(self.playPauseRequested.emit)
        self._next_button.clicked.connect(self.nextRequested.emit)
        self._mute_button.toggled.connect(self.muteToggled.emit)
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

        if self._scrubbing:
            return
        position = max(0, min(position_ms, self._duration if self._duration else position_ms))
        with self._block_position_updates():
            self._position_slider.setValue(position)
        self._elapsed_label.setText(self._format_ms(position))

    def set_playback_state(self, state: object) -> None:
        """Switch the play button icon based on *state*."""

        name = getattr(state, "name", None)
        if name == "PlayingState":
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
        self._play_button.setText("▶")

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _on_volume_changed(self, value: int) -> None:
        self.volumeChanged.emit(value)

    def _on_slider_pressed(self) -> None:
        self._scrubbing = True
        self._scrub_pending_value = self._position_slider.value()
        self._schedule_scrub_emit(immediate=True)
        self.scrubStarted.emit()

    def _on_slider_released(self) -> None:
        self._scrubbing = False
        self._scrub_timer.stop()
        self.seekRequested.emit(self._position_slider.value())
        self._scrub_pending_value = None
        self.scrubFinished.emit()

    def _on_slider_value_changed(self, value: int) -> None:
        if self._updating_position:
            return
        self._elapsed_label.setText(self._format_ms(value))
        if self._scrubbing:
            self._scrub_pending_value = value
            self._schedule_scrub_emit()
        else:
            self.seekRequested.emit(value)

    def sizeHint(self) -> QSize:  # pragma: no cover - Qt sizing
        base = super().sizeHint()
        return QSize(max(base.width(), 420), base.height())

    def is_scrubbing(self) -> bool:
        """Return whether the user is currently dragging the progress slider."""

        return self._scrubbing

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
    # Scrubbing helpers
    # ------------------------------------------------------------------
    def _schedule_scrub_emit(self, *, immediate: bool = False) -> None:
        if not self._scrubbing:
            return
        if immediate:
            self._emit_pending_scrub()
            return
        if self._scrub_timer.isActive():
            return
        self._scrub_timer.start(SCRUB_REFRESH_INTERVAL_MS)

    def _emit_pending_scrub(self) -> None:
        if self._scrub_pending_value is None:
            return
        value = self._scrub_pending_value
        self._scrub_pending_value = None
        self.seekRequested.emit(value)
        if self._scrubbing:
            self._scrub_timer.start(SCRUB_REFRESH_INTERVAL_MS)

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

    # ------------------------------------------------------------------
    # Styling helpers
    # ------------------------------------------------------------------
    def _create_tool_button(
        self, text: str, tooltip: str, *, checkable: bool = False
    ) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        button.setCheckable(checkable)
        button.setIconSize(QSize(28, 28))
        button.setMinimumSize(QSize(36, 36))
        return button

    def _apply_palette(self) -> None:
        common_style = (
            "QToolButton { color: white; font-size: 18px; padding: 6px; }"
            "QToolButton:pressed { background-color: rgba(255, 255, 255, 40);"
            " border-radius: 8px; }"
            "QToolButton:checked { background-color: rgba(255, 255, 255, 64); }"
        )
        slider_style = (
            "QSlider::groove:horizontal { height: 4px;"
            " background: rgba(255, 255, 255, 96); border-radius: 2px; }"
            "QSlider::sub-page:horizontal { background: white; border-radius: 2px; }"
            "QSlider::add-page:horizontal { background: rgba(255, 255, 255, 48);"
            " border-radius: 2px; }"
            "QSlider::handle:horizontal { background: white; width: 14px;"
            " margin: -6px 0; border-radius: 7px; }"
        )
        volume_style = slider_style.replace("4px", "3px").replace("14px", "12px")
        label_style = "color: white; font-size: 12px;"

        self.setStyleSheet(
            "PlayerBar { background-color: rgba(20, 20, 20, 170);"
            " border-radius: 14px; color: white; }"
            + common_style
        )
        self._position_slider.setStyleSheet(slider_style)
        self._volume_slider.setStyleSheet(volume_style)
        self._elapsed_label.setStyleSheet(label_style)
        self._duration_label.setStyleSheet(label_style)
