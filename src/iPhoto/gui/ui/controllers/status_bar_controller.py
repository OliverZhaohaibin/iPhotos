"""Helpers responsible for status-bar progress feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QProgressBar

from ..ui_main_window import ChromeStatusBar

class StatusBarController(QObject):
    """Manage progress feedback and transient messages in the status bar."""

    def __init__(
        self,
        status_bar: ChromeStatusBar,
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

    def handle_scan_finished(self, _root: Path, success: bool) -> None:
        """Restore the status bar once a scan completes."""

        # ``_root`` is guaranteed to be a :class:`Path` because the facade now
        # emits strongly typed signals.  The argument is intentionally unused
        # because the status bar only cares about the outcome of the scan.

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

    def handle_import_started(self, root: Path) -> None:
        """Display an indeterminate indicator when an import begins."""

        # The import workflow mirrors the rescan feedback so we reuse the same
        # progress bar widget.  Setting the context allows other handlers to
        # recognise that the UI is currently occupied with an import task.
        self._progress_context = "import"
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        self.show_message("Starting import…")

    def handle_import_progress(self, root: Path, current: int, total: int) -> None:
        """Update the progress bar while the worker copies files."""

        if self._progress_context != "import":
            return
        if total <= 0:
            # Keep the bar indeterminate when the worker cannot determine how
            # many items remain (for example during the initial copy).
            self._progress_bar.setRange(0, 0)
        else:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(max(0, min(current, total)))
        if 0 < current < total:
            self.show_message(f"Importing… ({current}/{total})")
        elif total > 0 and current >= total:
            # The worker emits a final update once all files are copied to let
            # the user know that the subsequent rescan is in progress.
            self.show_message("Finalising import by rescanning…")

    def handle_import_finished(self, root: Path | None, success: bool, message: str) -> None:
        """Reset the status bar once the import worker signals completion."""

        if self._progress_context == "import":
            self._progress_bar.setVisible(False)
            self._progress_bar.setRange(0, 0)
            self._progress_context = None
        self.show_message(message, 5000)

    def handle_move_started(self, source: Path, destination: Path) -> None:
        """Display an indeterminate indicator while files are being moved."""

        self._progress_context = "move"
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        self.show_message("Starting move…")

    def handle_move_progress(self, _source: Path, current: int, total: int) -> None:
        """Update the progress bar while the move worker processes files."""

        if self._progress_context != "move":
            return
        if total <= 0:
            self._progress_bar.setRange(0, 0)
        else:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(max(0, min(current, total)))
        if 0 < current < total:
            self.show_message(f"Moving… ({current}/{total})")
        elif total > 0 and current >= total:
            self.show_message("Finalising move by rescanning…")

    def handle_move_finished(
        self,
        _source: Path,
        _destination: Path,
        _success: bool,
        message: str,
    ) -> None:
        """Hide the progress bar and surface the worker's completion message."""

        if self._progress_context == "move":
            self._progress_bar.setVisible(False)
            self._progress_bar.setRange(0, 0)
            self._progress_context = None
        self.show_message(message, 5000)

