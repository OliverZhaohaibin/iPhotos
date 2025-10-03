"""Diagnose whether the current Python build supports PyCharm debugging."""

from __future__ import annotations

from iPhoto.utils.deps import debugger_prerequisites


def main() -> int:
    info = debugger_prerequisites()
    if info.has_ctypes:
        print("Debugger prerequisites satisfied: `_ctypes` is available.")
        return 0

    print("PyCharm debugging prerequisites missing:\n")
    print(info.message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
