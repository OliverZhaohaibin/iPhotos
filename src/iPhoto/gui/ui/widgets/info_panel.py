"""Floating window that displays EXIF metadata for the selected asset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Optional

from PySide6.QtCore import QDateTime, QLocale, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..icon import load_icon
from ..window_chrome import WINDOW_CONTROL_BUTTON_SIZE, WINDOW_CONTROL_GLYPH_SIZE


@dataclass
class _FormattedMetadata:
    """Pre-formatted strings used to populate the info panel labels."""

    name: str = ""
    timestamp: str = ""
    camera: str = ""
    lens: str = ""
    summary: str = ""
    exposure_line: str = ""
    is_video: bool = False


class InfoPanel(QWidget):
    """Small helper window that mirrors macOS Photos' info popover."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        # ``Qt.Window`` restores the platform-provided frame so the info panel behaves like a
        # traditional tool window, while ``Qt.Tool`` and ``Qt.WindowStaysOnTopHint`` preserve the
        # original floating behaviour.  Dropping the frameless flag honours the revised design
        # direction to keep the default decorations while still letting us style the close button.
        super().__init__(parent, Qt.Window | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Info")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumWidth(320)

        self._metadata: Optional[dict[str, Any]] = None
        self._current_rel: Optional[str] = None

        self._close_button = QToolButton(self)
        self._configure_close_button()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ``header_layout`` houses the custom close button while leaving the rest of the panel's
        # content untouched.  The stretch pushes the control to the right edge, giving the widget a
        # lightweight, title-bar style affordance even though the native frame is still present.
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)
        header_layout.addStretch(1)
        header_layout.addWidget(self._close_button)
        layout.addLayout(header_layout)

        self._filename_label = QLabel()
        self._filename_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._filename_label.setWordWrap(True)

        self._timestamp_label = QLabel()
        self._timestamp_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._timestamp_label.setWordWrap(True)

        self._camera_label = QLabel()
        self._camera_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._camera_label.setWordWrap(True)

        self._lens_label = QLabel()
        self._lens_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._lens_label.setWordWrap(True)

        self._summary_label = QLabel()
        self._summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._summary_label.setWordWrap(True)

        self._exposure_label = QLabel()
        self._exposure_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._exposure_label.setWordWrap(True)

        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)
        layout.addLayout(content_layout)

        content_layout.addWidget(self._filename_label)
        content_layout.addWidget(self._timestamp_label)

        metadata_frame = QWidget(self)
        metadata_layout = QVBoxLayout(metadata_frame)
        metadata_layout.setContentsMargins(0, 0, 0, 0)
        metadata_layout.setSpacing(6)
        metadata_layout.addWidget(self._camera_label)
        metadata_layout.addWidget(self._lens_label)
        metadata_layout.addWidget(self._summary_label)
        content_layout.addWidget(metadata_frame)

        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        content_layout.addWidget(separator)

        exposure_container = QWidget(self)
        exposure_layout = QHBoxLayout(exposure_container)
        exposure_layout.setContentsMargins(0, 0, 0, 0)
        exposure_layout.addWidget(self._exposure_label)
        content_layout.addWidget(exposure_container)

        content_layout.addStretch(1)

    def _configure_close_button(self) -> None:
        """Create a custom close button that matches the main window controls."""

        # Reuse the shared window-control metrics so the popup's chrome stays visually aligned with
        # the primary title bar buttons.  ``load_icon`` guarantees the SVG renders crisply at the
        # chosen size across platforms.
        self._close_button.setIcon(load_icon("red.close.circle.svg"))
        self._close_button.setIconSize(WINDOW_CONTROL_GLYPH_SIZE)
        self._close_button.setFixedSize(WINDOW_CONTROL_BUTTON_SIZE)
        self._close_button.setAutoRaise(True)
        self._close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._close_button.setToolTip("Close Info Panel")
        self._close_button.clicked.connect(self.close)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_asset_metadata(self, metadata: Mapping[str, Any]) -> None:
        """Populate the panel with information extracted from *metadata*."""

        self._metadata = dict(metadata)
        self._current_rel = str(metadata.get("rel") or metadata.get("name") or "") or None

        formatted = self._format_metadata(metadata)
        self._filename_label.setText(formatted.name)
        self._timestamp_label.setText(formatted.timestamp)
        self._camera_label.setVisible(bool(formatted.camera))
        self._camera_label.setText(formatted.camera)
        self._lens_label.setVisible(bool(formatted.lens))
        self._lens_label.setText(formatted.lens)
        self._summary_label.setVisible(bool(formatted.summary))
        self._summary_label.setText(formatted.summary)
        if formatted.exposure_line:
            self._exposure_label.setText(formatted.exposure_line)
        else:
            fallback = (
                "Detailed video information is unavailable."
                if formatted.is_video
                else "Detailed exposure information is unavailable."
            )
            self._exposure_label.setText(fallback)

    def clear(self) -> None:
        """Reset the panel to an empty state without hiding the window."""

        self._metadata = None
        self._current_rel = None
        for label in (
            self._filename_label,
            self._timestamp_label,
            self._camera_label,
            self._lens_label,
            self._summary_label,
            self._exposure_label,
        ):
            label.clear()
        self._exposure_label.setText("No metadata available for this item.")

    def current_rel(self) -> Optional[str]:
        """Return the relative path associated with the displayed asset."""

        return self._current_rel

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _format_metadata(self, metadata: Mapping[str, Any]) -> _FormattedMetadata:
        """Return a :class:`_FormattedMetadata` snapshot for *metadata*."""

        info = dict(metadata)
        name = self._resolve_name(info)
        timestamp = self._format_timestamp(info.get("dt"))
        camera = self._format_camera(info)
        lens = self._format_lens(info)
        is_video = bool(info.get("is_video"))
        summary = (
            self._format_video_summary(info)
            if is_video
            else self._format_photo_summary(info)
        )
        exposure_line = (
            self._format_video_details(info)
            if is_video
            else self._format_exposure_line(info)
        )
        return _FormattedMetadata(
            name=name,
            timestamp=timestamp,
            camera=camera,
            lens=lens if not is_video else "",
            summary=summary,
            exposure_line=exposure_line,
            is_video=is_video,
        )

    def _resolve_name(self, info: Mapping[str, Any]) -> str:
        """Return a human readable filename from *info*."""

        name = info.get("name")
        if isinstance(name, str) and name:
            return name
        rel = info.get("rel")
        if isinstance(rel, str) and rel:
            return Path(rel).name
        abs_path = info.get("abs")
        if isinstance(abs_path, str) and abs_path:
            return Path(abs_path).name
        return ""

    def _format_timestamp(self, value: Any) -> str:
        """Return *value* formatted using the current locale."""

        if not isinstance(value, str) or not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
        localized = parsed.astimezone()
        qt_datetime = QDateTime(localized)
        formatted = QLocale.system().toString(qt_datetime, QLocale.FormatType.LongFormat)
        if formatted:
            return formatted
        return localized.strftime("%Y-%m-%d %H:%M:%S")

    def _format_camera(self, info: Mapping[str, Any]) -> str:
        """Combine camera make and model if they are available."""

        make = info.get("make") if isinstance(info.get("make"), str) else None
        model = info.get("model") if isinstance(info.get("model"), str) else None
        if make and model:
            if model.lower().startswith(make.lower()):
                return model
            return f"{make} {model}"
        if model:
            return model
        if make:
            return make
        return ""

    def _format_lens(self, info: Mapping[str, Any]) -> str:
        """Return the lens description augmented with focal and aperture data."""

        lens = info.get("lens") if isinstance(info.get("lens"), str) else None
        focal_text = self._format_focal_length(info.get("focal_length"))
        aperture_text = self._format_aperture(info.get("f_number"))
        components = [component for component in (focal_text, aperture_text) if component]
        if lens and components:
            return f"{lens} — {' '.join(components)}"
        if lens:
            return lens
        if components:
            return " ".join(components)
        return ""

    def _format_photo_summary(self, info: Mapping[str, Any]) -> str:
        """Compose a single line summarising the image dimensions and size."""

        width = info.get("w")
        height = info.get("h")
        dimensions = ""
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            dimensions = f"{width} × {height}"

        size_text = self._format_filesize(info.get("bytes"))
        format_text = self._format_format(info)
        parts = [part for part in (dimensions, size_text, format_text) if part]
        return "    ".join(parts)

    def _format_video_summary(self, info: Mapping[str, Any]) -> str:
        """Summarise a video's dimensions, size, and codec in a single line."""

        width = info.get("w")
        height = info.get("h")
        dimensions = ""
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            dimensions = f"{width} × {height}"

        size_text = self._format_filesize(info.get("bytes"))
        codec_text = self._format_codec(info)
        parts = [part for part in (dimensions, size_text, codec_text) if part]
        return "    ".join(parts)

    def _format_exposure_line(self, info: Mapping[str, Any]) -> str:
        """Compose the ISO, focal length, EV, aperture, and shutter speed line."""

        iso_value = info.get("iso")
        iso_text = ""
        if isinstance(iso_value, (int, float)):
            iso_text = f"ISO {int(round(float(iso_value)))}"

        focal_text = self._format_focal_length(info.get("focal_length"))
        ev_text = self._format_exposure_comp(info.get("exposure_compensation"))
        aperture_text = self._format_aperture(info.get("f_number"))
        shutter_text = self._format_shutter(info.get("exposure_time"))

        parts = [part for part in (iso_text, focal_text, ev_text, aperture_text, shutter_text) if part]
        return "    ".join(parts)

    def _format_video_details(self, info: Mapping[str, Any]) -> str:
        """Compose the frame-rate and duration line for a video asset."""

        frame_rate_text = self._format_frame_rate(info.get("frame_rate"))
        duration_text = self._format_duration(info.get("dur"))
        codec_summary = self._format_codec(info)
        codec_text = ""
        # Show the codec twice only when the summary had no value; this keeps
        # the layout tidy while still surfacing the information somewhere.
        if not codec_summary:
            codec_text = self._format_format(info)

        parts = [part for part in (frame_rate_text, duration_text, codec_text) if part]
        return "    ".join(parts)

    def _format_focal_length(self, value: Any) -> str:
        """Return a formatted focal length string in millimetres."""

        numeric = self._coerce_float(value)
        if numeric is None or numeric <= 0:
            return ""
        if abs(numeric - round(numeric)) < 0.05:
            return f"{int(round(numeric))} mm"
        return f"{numeric:.1f} mm"

    def _format_aperture(self, value: Any) -> str:
        """Return a formatted aperture string (ƒ-number)."""

        numeric = self._coerce_float(value)
        if numeric is None or numeric <= 0:
            return ""
        return f"ƒ{self._format_decimal(numeric, precision=2)}"

    def _format_exposure_comp(self, value: Any) -> str:
        """Return exposure compensation in EV when available."""

        numeric = self._coerce_float(value)
        if numeric is None:
            return ""
        text = self._format_decimal(numeric, precision=2)
        return f"{text} ev"

    def _format_shutter(self, value: Any) -> str:
        """Return shutter speed formatted as a fraction when suitable."""

        numeric = self._coerce_float(value)
        if numeric is None or numeric <= 0:
            return ""
        if numeric >= 1:
            return f"{self._format_decimal(numeric, precision=2)} s"
        fraction = Fraction(numeric).limit_denominator(8000)
        approx = fraction.numerator / fraction.denominator
        if abs(approx - numeric) <= 1e-4:
            if fraction.numerator == 1:
                return f"1/{fraction.denominator} s"
            return f"{fraction.numerator}/{fraction.denominator} s"
        return f"{self._format_decimal(numeric, precision=4)} s"

    def _format_filesize(self, value: Any) -> str:
        """Return *value* expressed in human readable units."""

        numeric = self._coerce_float(value)
        if numeric is None or numeric <= 0:
            return ""
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(numeric)
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        if unit_index == 0:
            return f"{int(size)} {units[unit_index]}"

        rounded = round(size, 1)
        if float(rounded).is_integer():
            return f"{int(rounded)} {units[unit_index]}"
        return f"{rounded:.1f} {units[unit_index]}"

    def _format_codec(self, info: Mapping[str, Any]) -> str:
        """Return a readable codec label derived from the stored metadata."""

        codec_value = info.get("codec")
        if isinstance(codec_value, str):
            candidate = codec_value.strip()
            if not candidate:
                return ""
            if "," in candidate:
                candidate = candidate.split(",", 1)[0].strip()
            if "/" in candidate:
                candidate = candidate.split("/")[-1].strip()
            if "(" in candidate:
                candidate = candidate.split("(")[0].strip()
            normalized = candidate.replace(".", "").replace("-", "").replace(" ", "").upper()
            mapping = {
                "H264": "H.264",
                "AVC": "H.264",
                "AVC1": "H.264",
                "H265": "H.265",
                "HEVC": "HEVC",
                "X265": "H.265",
                "PRORES": "ProRes",
            }
            if normalized in mapping:
                return mapping[normalized]
            if candidate.isupper():
                return candidate
            if candidate.islower():
                return candidate.upper()
            return candidate
        return self._format_format(info)

    def _format_frame_rate(self, value: Any) -> str:
        """Return the frame-rate with two decimal places when available."""

        numeric = self._coerce_float(value)
        if numeric is None or numeric <= 0:
            return ""
        return f"{self._format_decimal(numeric, precision=2)} fps"

    def _format_duration(self, value: Any) -> str:
        """Return a short ``mm:ss`` or ``hh:mm:ss`` string for *value* seconds."""

        numeric = self._coerce_float(value)
        if numeric is None or numeric < 0:
            return ""
        total_seconds = int(round(numeric))
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"

    def _format_format(self, info: Mapping[str, Any]) -> str:
        """Return a short label describing the image format."""

        name = info.get("name") if isinstance(info.get("name"), str) else None
        if name:
            suffix = Path(name).suffix
            if suffix:
                extension = suffix.lstrip(".")
                if extension.lower() in {"heic", "heif"}:
                    return "HEIF"
                return extension.upper()
        mime = info.get("mime") if isinstance(info.get("mime"), str) else None
        if mime:
            subtype = mime.split("/")[-1]
            if subtype.lower() in {"heic", "heif"}:
                return "HEIF"
            return subtype.upper()
        return ""

    def _format_decimal(self, value: float, *, precision: int) -> str:
        """Return *value* formatted with ``precision`` decimal places."""

        text = f"{value:.{precision}f}"
        text = text.rstrip("0").rstrip(".")
        return text or "0"

    def _coerce_float(self, value: Any) -> Optional[float]:
        """Return *value* as ``float`` when it represents a numeric quantity."""

        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value)
            except ValueError:
                return None
        return None
