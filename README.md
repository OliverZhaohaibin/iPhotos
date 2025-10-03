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

### Troubleshooting PyCharm debugging

If PyCharm's debugger aborts with ``ImportError: DLL load failed while importing
_ctypes`` the active Python runtime lacks the native ``_ctypes`` extension. That
binary ships with official CPython builds but may be absent in some Anaconda
environments. Use the helper script to verify the requirement:

```bash
python scripts/check_debugger.py
```

When the check fails install the missing dependency and reinstall Python inside
your environment, for example:

```bash
conda install libffi
conda install python --force-reinstall
```

Alternatively switch the interpreter to a python.org build where ``_ctypes`` is
always available.
