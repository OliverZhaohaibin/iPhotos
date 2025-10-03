"""GUI entry point for the iPhoto desktop application."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

if __package__ is None or __package__ == "":  # pragma: no cover - script mode
    package_root = Path(__file__).resolve().parents[2]
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from iPhoto.appctx import AppContext
    from iPhoto.gui.ui.main_window import MainWindow
else:  # pragma: no cover - normal package execution
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
