"""GUI entry point for the iPhoto desktop application."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ..appctx import AppContext
from .ui.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    """Launch the Qt application and return the exit code."""

    arguments = list(sys.argv if argv is None else argv)
    app = QApplication(arguments)
    context = AppContext()
    window = MainWindow(context)
    window.show()
    # Allow opening an album directly via argv[1].
    if len(arguments) > 1:
        window.open_album_from_path(Path(arguments[1]))
    return app.exec()


if __name__ == "__main__":  # pragma: no cover - manual launch
    raise SystemExit(main())
