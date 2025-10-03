# iPhoto

Bring Mac-style Photos to Windows.

## Features

- Folder-native album management with JSON manifest files.
- Incremental directory scanning that caches metadata in `.iPhoto/index.jsonl`.
- Automatic Live Photo pairing stored in `.iPhoto/links.json`.
- CLI for initialising albums, rescanning, pairing, managing covers, featured assets, and generating reports.

## Getting Started

```bash
pip install -e .
iphoto init /path/to/album
iphoto scan /path/to/album
iphoto pair /path/to/album
```

Use `iphoto cover set`, `iphoto feature add|rm`, and `iphoto report` for additional management tasks.

### Launching the desktop UI

The project ships with a PySide6-based desktop interface. After installing the
package you can launch it with:

```bash
iphoto-gui
```

Optionally provide an album path to open immediately:

```bash
iphoto-gui /photos/LondonTrip
```

### External tools

Video thumbnail generation and duration metadata rely on the `ffmpeg` toolchain.
Install `ffmpeg`/`ffprobe` and ensure they are on your `PATH` so Windows users
receive motion previews instead of placeholders.

Image metadata and HEIC decoding fall back to Pillow when available. On some
Windows Python builds the optional `_ctypes` extension is missing, which prevents
Pillow from importing. In that case the application skips Pillow-backed features
and continues with basic placeholders; install a Python distribution that
includes `_ctypes` to re-enable rich previews.

### PyCharm debugging on Windows

PyCharm launches its debugger through the `pydevd` helper package which imports
`ctypes` early in the boot sequence. Lightweight Conda environments that omit
`_ctypes` would previously crash before the application started. The project now
ships with a compatibility shim (`src/pydevd_tracing.py`) that intercepts the
import, warns once, and falls back to a pure-Python tracing implementation so
you can still debug the application without reinstalling Python.
