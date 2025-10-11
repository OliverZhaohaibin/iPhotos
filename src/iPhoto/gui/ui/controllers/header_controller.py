"""Update helpers for the detail view header widgets."""

from __future__ import annotations

from calendar import month_name
from datetime import datetime, timezone
from typing import Optional

from dateutil.parser import isoparse
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel

from ..models.asset_model import AssetModel, Roles


class HeaderController:
    """Drive the location and timestamp labels shown above the player."""

    def __init__(self, location_label: QLabel, timestamp_label: QLabel) -> None:
        """Capture label widgets and prepare convenience fonts."""

        self._location_label = location_label
        self._timestamp_label = timestamp_label
        self._timestamp_default_font = QFont(self._timestamp_label.font())
        self._timestamp_single_line_font = QFont(self._timestamp_label.font())
        if self._timestamp_single_line_font.pointSize() > 0:
            self._timestamp_single_line_font.setPointSize(
                self._timestamp_single_line_font.pointSize() + 1
            )
        else:
            self._timestamp_single_line_font.setPointSize(14)
        self._timestamp_single_line_font.setBold(True)

    def clear(self) -> None:
        """Clear both labels and hide them from view."""

        self._location_label.clear()
        self._location_label.hide()
        self._timestamp_label.clear()
        self._timestamp_label.hide()
        self._timestamp_label.setFont(self._timestamp_default_font)

    def update_for_row(self, row: Optional[int], model: AssetModel) -> None:
        """Populate labels with metadata for ``row`` in ``model``."""

        if row is None or row < 0:
            self.clear()
            return
        index = model.index(row, 0)
        if not index.isValid():
            self.clear()
            return
        location_raw = index.data(Roles.LOCATION)
        location_text: Optional[str] = None
        if isinstance(location_raw, str):
            location_text = location_raw.strip() or None
        elif location_raw is not None:
            location_text = str(location_raw).strip() or None
        timestamp_text = self._format_timestamp(index.data(Roles.DT))
        self._apply_header_text(location_text, timestamp_text)

    def _apply_header_text(
        self, location: Optional[str], timestamp: Optional[str]
    ) -> None:
        """Render formatted location and timestamp strings."""

        if not timestamp:
            self.clear()
            return
        timestamp = timestamp.strip()
        if not timestamp:
            self.clear()
            return
        location = (location or "").strip() or None
        if location:
            self._location_label.setText(location)
            self._location_label.show()
            self._timestamp_label.setFont(self._timestamp_default_font)
        else:
            self._location_label.clear()
            self._location_label.hide()
            self._timestamp_label.setFont(self._timestamp_single_line_font)
        self._timestamp_label.setText(timestamp)
        self._timestamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._timestamp_label.show()

    def _format_timestamp(self, dt_value: object) -> Optional[str]:
        """Convert ISO-8601 strings into a friendly, localised label."""

        if not dt_value or not isinstance(dt_value, str):
            return None
        try:
            parsed = isoparse(dt_value)
        except (ValueError, TypeError):
            return None
        if parsed.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            parsed = parsed.replace(tzinfo=local_tz)
        localized = parsed.astimezone()
        month_label = (
            month_name[localized.month] if 0 <= localized.month < len(month_name) else ""
        )
        if not month_label:
            month_label = f"{localized.month:02d}"
        return f"{localized.day}. {month_label}, {localized:%H:%M}"
