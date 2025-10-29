"""Qt widget package for the iPhoto GUI."""

__all__ = ["MainWindow"]



def __getattr__(name: str) -> object:
    if name == "MainWindow":
        from .main_window import MainWindow as _MainWindow

        return _MainWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():  # pragma: no cover - trivial helper
    return sorted(__all__)
