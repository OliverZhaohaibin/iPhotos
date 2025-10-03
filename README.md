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
