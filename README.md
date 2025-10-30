# 📸 iPhoto
> Bring the macOS *Photos* experience to Windows — folder-native, non-destructive photo management with Live Photo, maps, and smart albums.

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey)
![Language](https://img.shields.io/badge/language-Python%203.10%2B-blue)
![Framework](https://img.shields.io/badge/framework-PySide6%20(Qt6)-orange)
![License](https://img.shields.io/badge/license-MIT-green)
[![GitHub Repo](https://img.shields.io/badge/github-iPhotos-181717?logo=github)](https://github.com/OliverZhaohaibin/iPhotos)

---

## 🌟 Overview

**iPhoto** is a **folder-native photo manager** inspired by macOS *Photos*.  
It organizes your media using lightweight JSON manifests and cache files —  
offering rich album functionality while **keeping all original files intact**.

Key highlights:
-  🗂 No database, no import step — every folder *is* an album.
- ⚙️ JSON-based manifests record “human decisions” (cover, featured, order).
- 🧠 Smart incremental scanning & caching for fast startup.
- 🎥 Full **Live Photo** pairing and playback support.
- 🗺 Map view that visualizes GPS metadata across all photos & videos.
![Main interface](docs/mainview.png)
![Preview interface](docs/preview.png)
---

## ✨ Features

### 🗺 Location View
Displays your photo footprints on an interactive map, clustering nearby photos by GPS metadata.
![Location interface](docs/map1.png)
![Location interface](docs/map2.png)
### 🎞 Live Photo Support
Seamlessly pairs HEIC/JPG and MOV files using Apple’s `ContentIdentifier`.  
A “LIVE” badge appears on still photos — click to play the motion video inline.

### 🧩 Smart Albums
The sidebar provides an auto-generated **Basic Library**, grouping photos into:
`All Photos`, `Videos`, `Live Photos`, `Favorites`, and `Recently Deleted`.

### 🖼 Immersive Detail View
An elegant viewer with a filmstrip navigator and floating playback bar for videos.

### ℹ️ Floating Info Panel
Toggle a floating metadata panel showing EXIF, camera/lens info, exposure, aperture, focal length, file size, and more.
![Info interface](docs/info1.png)
### 💬 Rich Interactions
- Drag & drop files from Explorer/Finder directly into albums.
- Multi-selection & context menus for Copy, Show in Folder, Move, Delete, Restore.
- Smooth thumbnail transitions and macOS-like album navigation.

---

## ⚙️ Core Engine

| Concept | Description |
|----------|--------------|
| **Folder = Album** | Managed via `.iphoto.album.json` manifest files. |
| **Incremental Scan** | Scans new/changed files and caches results in `.iPhoto/index.jsonl`. |
| **Live Pairing** | Auto-matches Live Photos using `ContentIdentifier` or time proximity. |
| **Reverse Geocoding** | Converts GPS coordinates into human-readable locations (e.g. “London”). |
| **Command Line Tool** | Provides a `iphoto` CLI for album init, scan, pairing, and report generation. |

---

## 🧰 Command-Line Usage

```bash
# 1️⃣ Install dependencies
pip install -e .

# 2️⃣ Initialize an album (creates .iphoto.album.json)
iphoto init /path/to/album

# 3️⃣ Scan files and build index
iphoto scan /path/to/album

# 4️⃣ Pair Live Photos (HEIC/JPG + MOV)
iphoto pair /path/to/album

# 5️⃣ Manage album properties
iphoto cover set /path/to/album IMG_1234.HEIC
iphoto feature add /path/to/album museum/IMG_9999.HEIC#live
iphoto report /path/to/album
```

## 🖥 GUI Interface (PySide6 / Qt6)

After installation, you can launch the full desktop interface:

```bash
iphoto-gui
```
Or directly open a specific album:

```bash
iphoto-gui /photos/LondonTrip
```
### GUI Highlights

- **Album Sidebar:** Hierarchical folder view with favorites & smart albums.  
- **Asset Grid:** Adaptive thumbnail layout, selection, and lazy-loaded previews.  
- **Map View:** Interactive GPS clustering with tile caching.  
- **Detail Viewer:** Filmstrip navigation and playback controls.  
- **Metadata Panel:** Collapsible EXIF + QuickTime info panel.  
- **Context Menu:** Copy, Move, Delete, Restore.
## 🧱 Project Structure

The source code resides under the `src/iPhoto/` directory and is divided into two main parts — **Core Backend** and **GUI**.

---

### 1️⃣ Core Backend (`src/iPhoto/`)

This section is pure Python logic and **does not depend** on any GUI framework (such as PySide6).

| File / Module | Description |
|----------------|-------------|
| **`app.py`** | High-level backend **Facade** coordinating all core modules, used by both CLI and GUI. |
| **`cli.py`** | Typer-based command-line entry point that parses user commands and invokes methods from `app.py`. |
| **`models/`** | Defines the main data structures such as `Album` (manifest read/write) and `LiveGroup`. |
| **`io/`** | Handles filesystem interaction, mainly `scanner.py` (file scanning) and `metadata.py` (metadata reading). |
| **`core/`** | Core algorithmic logic such as `pairing.py` (Live Photo pairing algorithm). |
| **`cache/`** | Manages disposable cache files, including `index_store.py` (read/write `index.jsonl`) and `lock.py` (file-level locking). |
| **`utils/`** | General utilities, especially wrappers for external tools (`exiftool.py`, `ffmpeg.py`). |
| **`schemas/`** | JSON Schema definitions, e.g., `album.schema.json`. |

---

### 2️⃣ GUI Layer (`src/iPhoto/gui/`)

This is the PySide6-based desktop application layer, which depends on the backend core.

| File / Module | Description |
|----------------|-------------|
| **`main.py`** | Entry point for the GUI application (`iphoto-gui` command). |
| **`appctx.py`** | Defines `AppContext`, a shared global state manager for settings, library manager, and the backend Facade instance. |
| **`facade.py`** | Defines `AppFacade` (a `QObject`) — the bridge between the GUI and backend. It wraps the backend `app` module and uses Qt **signals/slots** to decouple backend operations (scan/import) from the GUI event loop. |
| **`services/`** | Encapsulates complex, stateful background operations such as `AssetMoveService`, `AssetImportService`, and `LibraryUpdateService`. These are coordinated by `AppFacade`. |
| **`background_task_manager.py`** | Manages the `QThreadPool`, runs tasks submitted by `services`, and handles pausing/resuming of file watchers. |
| **`ui/`** | Contains all UI components: windows, controllers, models, and custom widgets. |
| ├─ **`main_window.py`** |— Implementation of the main `QMainWindow`. |
| ├─ **`ui_main_window.py`** |— Auto-generated from Qt Designer (`pyside6-uic`), defining all widgets. |
| ├─ **`controllers/`** |— The “brain” of the GUI (MVC pattern). `main_controller.py` orchestrates all subcontrollers (e.g., `NavigationController`, `PlaybackController`) and connects all signals and slots. |
| ├─ **`models/`** |— Qt **Model-View** data models such as `AssetListModel` and `AlbumTreeModel`. |
| ├─ **`widgets/`** |— Reusable custom QWidget components such as `AlbumSidebar`, `PhotoMapView`, and `PlayerBar`. |
| └─ **`tasks/`**| — `QRunnable` implementations for background tasks, e.g., `ThumbnailLoader` and `ScannerWorker`. |

---
### 3️⃣ Map Component (`src/iPhoto/gui/maps/`)

This directory contains a semi-independent **map rendering module** used by the `PhotoMapView` widget.

| File / Module | Description |
|----------------|-------------|
| **`map_widget/`** | Contains the core map widget classes and rendering logic. |
| ├─ **`MapWidget.py`** | Main map widget class managing user interaction and viewport state. |
| ├─ **`MapGLWidget.py`** | OpenGL-based rendering widget for efficient tile and vector drawing. |
| ├─ **`MapRenderer.py`** | Responsible for rendering map tiles and vector layers. |
| └─ **`TileManager.py`** | Handles tile fetching, caching, and lifecycle management. |
| **`style_resolver.py`** | Parses MapLibre style sheets (`style.json`) and applies style rules to the renderer. |
| **`tile_parser.py`** | Parses `.pbf` vector tile files and converts them into drawable map primitives. |
---
This modular separation ensures:
- ✅ **Backend logic** remains independent and easily testable.  
- ✅ **GUI architecture** follows MVC principles (Controllers coordinate Models and Widgets).  
- ✅ **Background tasks** are handled asynchronously for smooth user interaction.


## 🧱 Module Dependency Hierarchy

The project follows a **strict layered architecture** to ensure a clear separation between **core logic** and the **UI layer**.

### 🧩 Core Backend (Pure Python)

- **Base Layer:**  
  `utils`, `errors`, `config`, `models`, and `schemas` — foundational modules with minimal interdependencies.

- **Middle Layer:**  
  `io`, `core`, and `cache` depend on the base layer and implement file operations, metadata extraction, and algorithmic logic.

- **Facade Layer:**  
  `app.py` serves as the backend facade, coordinating `core`, `io`, `cache`, and `models`.

---

### 🪟 GUI Layer (PySide6)

- **GUI Facade (`gui/facade.py`):**  
  The bridge between backend logic (`app.py`) and the Qt event world.  
  It exposes backend functionality via Qt signals/slots.

- **Services (`gui/services/`):**  
  Depend on `gui/background_task_manager.py` and `app.py`.  
  Handle long-running or asynchronous tasks such as scanning, importing, and moving assets.

- **Controllers (`gui/ui/controllers/`):**  
  Depend on `gui/facade.py` and `gui/services/` to **trigger actions**,  
  and on `gui/ui/models/` and `gui/ui/widgets/` to **update views**.

- **Models & Widgets (`gui/ui/models/`, `gui/ui/widgets/`):**  
  Passive components — they should **not** depend on Controllers or Services.  
  Communication happens solely via Qt signals.

- **Tasks (`gui/ui/tasks/`):**  
  Contain `QRunnable` worker classes that depend on functions from `core` and `io`,  
  such as `thumbnail_loader.py` (which uses `ffmpeg.py`).

---

This architecture ensures:
- The backend remains fully testable and independent.
- GUI logic (Controllers) is decoupled from rendering (Widgets) and data state (Models).

---
## 🧩 External Tools

| Tool | Purpose |
|------|----------|
| **ExifTool** | Reads EXIF, GPS, QuickTime, and Live Photo metadata. |
| **FFmpeg / FFprobe** | Generates video thumbnails & parses video info. |

> Ensure both are available in your system `PATH`.

Python dependencies (e.g., `Pillow`, `reverse-geocoder`) are auto-installed via `pyproject.toml`.

---

## 🧪 Development

### Run Tests

```bash
pytest
```
### Code Style

- **Linters & Formatters:** `ruff`, `black`, and `mypy`  
- **Line length:** ≤ 100 characters  
- **Type hints:** use full annotations (e.g., `Optional[str]`, `list[Path]`, `dict[str, Any]`)
## 📄 License

**MIT License © 2025**  
Created by **Haibin Zhao (OliverZhaohaibin)**  

> *iPhoto — A folder-native, human-readable, and fully rebuildable photo system.*  
> *No imports. No database. Just your photos, organized elegantly.*
