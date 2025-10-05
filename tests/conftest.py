import sys
from types import ModuleType
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Ensure the project sources are importable as ``iPhotos.src`` to match legacy tests.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if "iPhotos" not in sys.modules:
    pkg = ModuleType("iPhotos")
    pkg.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules["iPhotos"] = pkg
