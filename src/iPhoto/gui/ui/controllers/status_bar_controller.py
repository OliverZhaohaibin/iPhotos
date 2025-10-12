"""Helpers responsible for status-bar progress feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QProgressBar, QStatusBar


class StatusBarController(QObject):
    """Manage progress feedback and transient messages in the status bar."""

    def __init__(
        self,
        status_bar: QStatusBar,
        progress_bar: QProgressBar,
        rescan_action: QAction | None,
    ) -> None:
        super().__init__(status_bar)
        self._status_bar = status_bar
        self._progress_bar = progress_bar
        self._rescan_action = rescan_action
        self._progress_context: Optional[str] = None

    # Generic helpers -------------------------------------------------
    def show_message(self, message: str, timeout_ms: int | None = None) -> None:
        """Proxy :meth:`QStatusBar.showMessage` for the owning controller."""

        if timeout_ms is None:
            self._status_bar.showMessage(message)
        else:
            self._status_bar.showMessage(message, timeout_ms)

    def begin_scan(self) -> None:
        """Prepare the UI for a long-running scan operation."""

        self._progress_context = "scan"
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        if self._rescan_action is not None:
            self._rescan_action.setEnabled(False)
        self.show_message("Starting scan…")

    # Facade callbacks ------------------------------------------------
    def handle_scan_progress(self, root: Path, current: int, total: int) -> None:
        """Update the progress bar while the library is being scanned."""

        if self._progress_context not in {"scan", None}:
            return
        if self._progress_context is None:
            # A scan triggered from outside the controller started without
            # calling :meth:`begin_scan`; bootstrap the UI lazily.
            self.begin_scan()

        if total < 0:
            self._progress_bar.setRange(0, 0)
            self.show_message("Scanning… (counting files)")
        elif total == 0:
            self._progress_bar.setRange(0, 0)
            self.show_message("Scanning… (no files found)")
        else:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(max(0, min(current, total)))
            self.show_message(f"Scanning… ({current}/{total})")
        self._progress_bar.setVisible(True)

    def handle_scan_finished(self, root: Path | None, success: bool) -> None:
        """Restore the status bar once a scan completes."""

        if self._progress_context == "scan":
            self._progress_bar.setVisible(False)
            self._progress_bar.setRange(0, 0)
            self._progress_context = None
        if self._rescan_action is not None:
            self._rescan_action.setEnabled(True)
        message = "Scan complete." if success else "Scan failed."
        self.show_message(message, 5000)

    def handle_load_started(self, root: Path) -> None:
        """Show an indeterminate progress indicator while assets load."""

        self._progress_context = "load"
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        self.show_message("Loading items…")

    def handle_load_progress(self, root: Path, current: int, total: int) -> None:
        """Update the progress bar while assets stream into the model."""

        if self._progress_context != "load":
            return
        if total <= 0:
            self._progress_bar.setRange(0, 0)
        else:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(max(0, min(current, total)))
        if total > 0:
            self.show_message(f"Loading items… ({current}/{total})")

    def handle_load_finished(self, root: Path, success: bool) -> None:
        """Hide the progress bar once loading wraps up."""

        if self._progress_context != "load":
            return
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 0)
        self._progress_context = None
        message = "Album loaded." if success else "Failed to load album."
        self.show_message(message, 5000)

