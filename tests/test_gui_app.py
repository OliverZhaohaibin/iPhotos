from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover - pillow missing or broken
    pytest.skip(
        f"Pillow unavailable for GUI tests: {exc}",
        allow_module_level=True,
    )

pytest.importorskip("PySide6", reason="PySide6 is required for GUI tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)
from PySide6.QtCore import Qt, QSize, QObject, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import (
    QApplication,  # type: ignore  # noqa: E402
    QLabel,
    QSlider,
    QStackedWidget,
    QStatusBar,
    QToolButton,
    QWidget,
)

from iPhotos.src.iPhoto.gui.facade import AppFacade
from iPhotos.src.iPhoto.gui.ui.controllers.detail_ui_controller import DetailUIController
from iPhotos.src.iPhoto.gui.ui.controllers.header_controller import HeaderController
from iPhotos.src.iPhoto.gui.ui.controllers.player_view_controller import (
    PlayerViewController,
)
from iPhotos.src.iPhoto.gui.ui.controllers.playback_controller import PlaybackController
from iPhotos.src.iPhoto.gui.ui.controllers.playback_state_manager import (
    PlaybackStateManager,
)
from iPhotos.src.iPhoto.gui.ui.controllers.preview_controller import PreviewController
from iPhotos.src.iPhoto.gui.ui.controllers.view_controller import ViewController
from iPhotos.src.iPhoto.gui.ui.models.asset_model import AssetModel, Roles
from iPhotos.src.iPhoto.gui.ui.media.playlist_controller import PlaylistController
from iPhotos.src.iPhoto.gui.ui.models.spacer_proxy_model import SpacerProxyModel
from iPhotos.src.iPhoto.gui.ui.tasks.thumbnail_loader import ThumbnailJob
from iPhotos.src.iPhoto.gui.ui.widgets.gallery_grid_view import GalleryGridView
from iPhotos.src.iPhoto.gui.ui.widgets.filmstrip_view import FilmstripView
from iPhotos.src.iPhoto.gui.ui.widgets.image_viewer import ImageViewer
from iPhotos.src.iPhoto.gui.ui.widgets.player_bar import PlayerBar
from iPhotos.src.iPhoto.gui.ui.widgets.video_area import VideoArea
from iPhotos.src.iPhoto.gui.ui.widgets.live_badge import LiveBadge
from iPhotos.src.iPhoto.config import WORK_DIR_NAME
from iPhotos.src.iPhoto.library.manager import LibraryManager
from iPhotos.src.iPhoto.models.album import Album


def _create_image(path: Path) -> None:
    image = Image.new("RGB", (8, 8), color="blue")
    image.save(path)


class _StubMediaController(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    volumeChanged = Signal(int)
    mutedChanged = Signal(bool)
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.loaded: Path | None = None
        self.play_calls = 0
        self.stopped = False
        self.seeked_to: int | None = None
        self._volume = 50
        self._muted = False
        self._state = SimpleNamespace(name="StoppedState")

    def load(self, path: Path) -> None:
        self.loaded = path

    def play(self) -> None:
        self.play_calls += 1
        self._state = SimpleNamespace(name="PlayingState")

    def stop(self) -> None:
        self.stopped = True
        self._state = SimpleNamespace(name="StoppedState")

    def pause(self) -> None:
        self._state = SimpleNamespace(name="PausedState")

    def toggle(self) -> None:
        if getattr(self._state, "name", "") == "PlayingState":
            self.pause()
        else:
            self.play()

    def seek(self, position_ms: int) -> None:
        self.seeked_to = position_ms

    def set_volume(self, volume: int) -> None:
        self._volume = volume

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        self.mutedChanged.emit(muted)

    def volume(self) -> int:
        return self._volume

    def is_muted(self) -> bool:
        return self._muted

    def playback_state(self) -> object:
        return self._state

    def current_source(self) -> Path | None:
        return self.loaded


class _StubPreviewWindow:
    def __init__(self) -> None:
        self.closed: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.previewed: list[tuple[object, object]] = []

    def close_preview(self, *args, **kwargs) -> None:
        self.closed.append((args, kwargs))

    def show_preview(self, *args, **kwargs) -> None:
        if not args:
            return
        source = args[0]
        rect = args[1] if len(args) > 1 else None
        self.previewed.append((source, rect))


class _StubDialog:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def show_error(self, message: str) -> None:
        self.errors.append(message)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_facade_open_album_emits_signals(tmp_path: Path, qapp: QApplication) -> None:
    asset = tmp_path / "IMG_1001.JPG"
    _create_image(asset)
    facade = AppFacade()
    received: list[str] = []
    facade.albumOpened.connect(lambda _: received.append("opened"))
    facade.indexUpdated.connect(lambda _: received.append("index"))
    facade.linksUpdated.connect(lambda _: received.append("links"))
    album = facade.open_album(tmp_path)
    qapp.processEvents()
    assert album is not None
    assert (tmp_path / ".iPhoto" / "index.jsonl").exists()
    assert "opened" in received and "index" in received


def test_facade_rescan_emits_links(tmp_path: Path, qapp: QApplication) -> None:
    asset = tmp_path / "IMG_1101.JPG"
    _create_image(asset)
    facade = AppFacade()
    facade.open_album(tmp_path)
    spy = QSignalSpy(facade.linksUpdated)
    facade.rescan_current()
    qapp.processEvents()
    assert spy.count() >= 1


def test_asset_model_populates_rows(tmp_path: Path, qapp: QApplication) -> None:
    asset = tmp_path / "IMG_2001.JPG"
    _create_image(asset)
    facade = AppFacade()
    model = AssetModel(facade)
    facade.open_album(tmp_path)
    qapp.processEvents()
    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert model.data(index, Roles.REL) == "IMG_2001.JPG"
    assert model.data(index, Roles.FEATURED) is False
    spy = QSignalSpy(model.dataChanged)
    decoration = model.data(index, Qt.DecorationRole)
    assert isinstance(decoration, QPixmap)
    assert not decoration.isNull()
    spy.wait(500)
    qapp.processEvents()
    refreshed = model.data(index, Qt.DecorationRole)
    assert isinstance(refreshed, QPixmap)
    assert not refreshed.isNull()
    thumbs_dir = tmp_path / WORK_DIR_NAME / "thumbs"
    for _ in range(20):
        qapp.processEvents()
        if thumbs_dir.exists() and any(thumbs_dir.iterdir()):
            break
        time.sleep(0.05)
    assert thumbs_dir.exists()
    assert any(thumbs_dir.iterdir())


def test_facade_toggle_featured_updates_model(tmp_path: Path, qapp: QApplication) -> None:
    asset = tmp_path / "IMG_2101.JPG"
    _create_image(asset)
    facade = AppFacade()
    model = AssetModel(facade)
    facade.open_album(tmp_path)
    qapp.processEvents()

    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert not bool(model.data(index, Roles.FEATURED))

    add_spy = QSignalSpy(model.dataChanged)
    added = facade.toggle_featured("IMG_2101.JPG")
    qapp.processEvents()
    assert added is True
    assert bool(model.data(index, Roles.FEATURED))
    assert add_spy.count() >= 1

    manifest_path = tmp_path / ".iphoto.album.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest.get("featured") == ["IMG_2101.JPG"]

    remove_spy = QSignalSpy(model.dataChanged)
    removed = facade.toggle_featured("IMG_2101.JPG")
    qapp.processEvents()
    assert removed is False
    assert not bool(model.data(index, Roles.FEATURED))
    assert remove_spy.count() >= 1

    manifest = json.loads(manifest_path.read_text())
    assert manifest.get("featured") == []


def test_toggle_featured_does_not_emit_album_opened(tmp_path: Path, qapp: QApplication) -> None:
    asset = tmp_path / "IMG_2102.JPG"
    _create_image(asset)
    facade = AppFacade()
    AssetModel(facade)  # Ensure the list model is initialised for the album.
    facade.open_album(tmp_path)
    qapp.processEvents()

    opened_spy = QSignalSpy(facade.albumOpened)

    facade.toggle_featured("IMG_2102.JPG")
    qapp.processEvents()

    assert opened_spy.count() == 0


def test_toggle_featured_syncs_library_manifest(tmp_path: Path, qapp: QApplication) -> None:
    """Favorites toggled in sub-albums must appear in the library view."""

    library_root = tmp_path / "Library"
    sub_album = library_root / "Trip"
    library_root.mkdir()
    sub_album.mkdir()

    asset = sub_album / "IMG_9101.JPG"
    _create_image(asset)

    # Create manifests so both the library root and the sub-album behave like
    # real albums during the test.  ``Album.open`` lazily builds a manifest
    # structure when one is missing, therefore saving immediately writes the
    # expected on-disk representation.
    Album.open(library_root).save()
    Album.open(sub_album).save()

    library = LibraryManager()
    library.bind_path(library_root)

    facade = AppFacade()
    facade.bind_library(library)
    model = AssetModel(facade)

    # Start in the sub-album where the user would typically toggle the
    # favorite flag.  ``processEvents`` allows the background loader to ingest
    # the freshly scanned index rows.
    facade.open_album(sub_album)
    for _ in range(10):
        qapp.processEvents()
        if model.rowCount() > 0:
            break

    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert bool(model.data(index, Roles.FEATURED)) is False

    became_featured = facade.toggle_featured("IMG_9101.JPG")
    qapp.processEvents()
    assert became_featured is True
    assert bool(model.data(index, Roles.FEATURED)) is True

    # The sub-album manifest keeps its local relative path, while the library
    # root tracks the asset via the fully qualified path.  Both perspectives
    # must reflect the update for the Favorites collection to work.
    sub_manifest = Album.open(sub_album)
    assert "IMG_9101.JPG" in sub_manifest.manifest.get("featured", [])
    root_manifest = Album.open(library_root)
    assert "Trip/IMG_9101.JPG" in root_manifest.manifest.get("featured", [])

    # Switching to the library root mirrors the behavior of choosing the
    # "Favorites" node.  The model should now expose the nested asset with the
    # global relative path and the featured flag enabled.
    facade.open_album(library_root)
    for _ in range(10):
        qapp.processEvents()
        if model.rowCount() > 0:
            break

    rel_values = [
        model.data(model.index(row, 0), Roles.REL)
        for row in range(model.rowCount())
    ]
    assert "Trip/IMG_9101.JPG" in rel_values
    root_index = next(
        (
            model.index(row, 0)
            for row in range(model.rowCount())
            if rel_values[row] == "Trip/IMG_9101.JPG"
        ),
        None,
    )
    assert root_index is not None
    assert bool(model.data(root_index, Roles.FEATURED)) is True

    # Toggling the favorite off from the sub-album must clear both manifests so
    # the Favorites collection stops listing the asset.
    facade.open_album(sub_album)
    for _ in range(10):
        qapp.processEvents()
        if model.rowCount() > 0:
            break

    reverted = facade.toggle_featured("IMG_9101.JPG")
    qapp.processEvents()
    assert reverted is False

    sub_manifest = Album.open(sub_album)
    assert "IMG_9101.JPG" not in sub_manifest.manifest.get("featured", [])
    root_manifest = Album.open(library_root)
    assert "Trip/IMG_9101.JPG" not in root_manifest.manifest.get("featured", [])


def test_playlist_selection_survives_favorites_filter_changes(
    tmp_path: Path, qapp: QApplication
) -> None:
    """Removing a favorite should advance to the next playable asset."""

    first = tmp_path / "IMG_7001.JPG"
    second = tmp_path / "IMG_7002.JPG"
    _create_image(first)
    _create_image(second)

    facade = AppFacade()
    model = AssetModel(facade)
    facade.open_album(tmp_path)
    for _ in range(10):
        qapp.processEvents()
        if model.rowCount() == 2:
            break

    assert model.rowCount() == 2

    assert facade.toggle_featured(first.name) is True
    assert facade.toggle_featured(second.name) is True
    qapp.processEvents()

    model.set_filter_mode("favorites")
    qapp.processEvents()
    assert model.rowCount() == 2

    playlist = PlaylistController()
    playlist.bind_model(model)
    selected = playlist.set_current(0)
    assert selected is not None

    removed = facade.toggle_featured(first.name)
    assert removed is False
    qapp.processEvents()

    assert model.rowCount() == 1
    assert playlist.current_row() == 0
    current_source = playlist.current_source()
    assert current_source is not None
    assert current_source.name == second.name
def test_asset_model_filters_videos(tmp_path: Path, qapp: QApplication) -> None:
    image = tmp_path / "IMG_3001.JPG"
    video = tmp_path / "CLIP_0001.MP4"
    _create_image(image)
    video.write_bytes(b"")

    facade = AppFacade()
    model = AssetModel(facade)
    facade.open_album(tmp_path)
    qapp.processEvents()

    assert model.rowCount() == 2
    model.set_filter_mode("videos")
    qapp.processEvents()
    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert bool(model.data(index, Roles.IS_VIDEO))

    model.set_filter_mode(None)
    qapp.processEvents()
    assert model.rowCount() == 2


def test_asset_model_exposes_live_motion_abs(tmp_path: Path, qapp: QApplication) -> None:
    still = tmp_path / "IMG_4001.JPG"
    video = tmp_path / "IMG_4001.MOV"
    _create_image(still)
    video.write_bytes(b"\x00")
    timestamp = time.time() - 120
    os.utime(still, (timestamp, timestamp))
    os.utime(video, (timestamp, timestamp))

    facade = AppFacade()
    model = AssetModel(facade)
    facade.open_album(tmp_path)
    qapp.processEvents()

    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert bool(model.data(index, Roles.IS_LIVE))
    assert model.data(index, Roles.LIVE_MOTION_REL) == "IMG_4001.MOV"
    motion_abs = model.data(index, Roles.LIVE_MOTION_ABS)
    assert isinstance(motion_abs, str)
    assert motion_abs.endswith("IMG_4001.MOV")
    assert Path(motion_abs).exists()


def test_asset_model_pairs_live_when_links_missing(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    still = tmp_path / "IMG_4101.JPG"
    video = tmp_path / "IMG_4101.MOV"
    _create_image(still)
    video.write_bytes(b"\x00")
    timestamp = time.time() - 90
    os.utime(still, (timestamp, timestamp))
    os.utime(video, (timestamp, timestamp))

    from iPhotos.src.iPhoto.gui.ui.models import asset_list_model as alm

    monkeypatch.setattr(alm, "load_live_map", lambda _: {})

    facade = AppFacade()
    model = AssetModel(facade)
    facade.open_album(tmp_path)
    qapp.processEvents()

    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert bool(model.data(index, Roles.IS_LIVE))
    assert model.data(index, Roles.LIVE_MOTION_REL) == "IMG_4101.MOV"


def test_playback_controller_autoplays_live_photo(tmp_path: Path, qapp: QApplication) -> None:
    still = tmp_path / "IMG_5001.JPG"
    video = tmp_path / "IMG_5001.MOV"
    _create_image(still)
    video.write_bytes(b"\x00")
    timestamp = time.time() - 60
    os.utime(still, (timestamp, timestamp))
    os.utime(video, (timestamp, timestamp))

    facade = AppFacade()
    model = AssetModel(facade)
    facade.open_album(tmp_path)
    qapp.processEvents()

    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert bool(index.data(Roles.IS_LIVE))
    motion_abs_raw = index.data(Roles.LIVE_MOTION_ABS)
    assert isinstance(motion_abs_raw, str)
    motion_abs = Path(motion_abs_raw)
    assert motion_abs.exists()

    playlist = PlaylistController()
    playlist.bind_model(model)

    media = _StubMediaController()
    player_bar = PlayerBar()
    video_area = VideoArea()
    grid_view = GalleryGridView()
    filmstrip_view = FilmstripView()
    grid_view.setModel(model)

    # The production UI inserts spacer tiles before and after the first asset so
    # the current item stays centered.  ``SpacerProxyModel`` mirrors that
    # behaviour to keep the controller logic operating on the same indices the
    # real window exposes.
    filmstrip_model = SpacerProxyModel()
    filmstrip_model.setSourceModel(model)
    filmstrip_view.setModel(filmstrip_model)

    player_stack = QStackedWidget()
    placeholder = QLabel("placeholder")
    image_viewer = ImageViewer()
    player_stack.addWidget(placeholder)
    player_stack.addWidget(image_viewer)
    player_stack.addWidget(video_area)
    live_badge = LiveBadge(player_stack)
    live_badge.hide()
    view_stack = QStackedWidget()
    gallery_page = QWidget()
    detail_page = QWidget()
    view_stack.addWidget(gallery_page)
    view_stack.addWidget(detail_page)
    status_bar = QStatusBar()
    preview_window = _StubPreviewWindow()
    dialog = _StubDialog()
    location_label = QLabel()
    timestamp_label = QLabel()
    favorite_button = QToolButton()
    zoom_widget = QWidget()
    zoom_slider = QSlider(Qt.Orientation.Horizontal)
    zoom_in_button = QToolButton()
    zoom_out_button = QToolButton()

    # Construct the layered controllers that ``PlaybackController`` depends on.
    # Each helper mirrors the real application wiring so the behaviour under test
    # reflects production signal routing rather than shortcutting widget access.
    player_view_controller = PlayerViewController(
        player_stack,
        image_viewer,
        video_area,
        placeholder,
        live_badge,
    )
    view_controller = ViewController(
        view_stack,
        gallery_page,
        detail_page,
    )
    header_controller = HeaderController(
        location_label,
        timestamp_label,
    )
    detail_ui = DetailUIController(
        model,
        filmstrip_view,
        player_view_controller,
        player_bar,
        view_controller,
        header_controller,
        favorite_button,
        zoom_widget,
        zoom_slider,
        zoom_in_button,
        zoom_out_button,
        status_bar,
    )
    preview_controller = PreviewController(preview_window)  # type: ignore[arg-type]
    state_manager = PlaybackStateManager(
        media,
        playlist,
        model,
        detail_ui,
        dialog,  # type: ignore[arg-type]
    )
    controller = PlaybackController(
        model,
        media,
        playlist,
        grid_view,
        view_controller,
        detail_ui,
        state_manager,
        preview_controller,
        facade,
    )

    # The preview controller now owns the long-press workflow, so bind it to the
    # grid and filmstrip views to mimic how the main window connects the shared
    # preview window.
    preview_controller.bind_view(grid_view)
    preview_controller.bind_view(filmstrip_view)
    playlist.currentChanged.connect(controller.handle_playlist_current_changed)
    playlist.sourceChanged.connect(controller.handle_playlist_source_changed)

    # Emit the long-press signal directly to simulate a user previewing the Live
    # Photo before activating it.  ``PreviewController`` listens to the signal
    # and routes the preview request to the shared window.
    grid_view.requestPreview.emit(index)
    qapp.processEvents()
    assert preview_window.previewed
    preview_source, _ = preview_window.previewed[-1]
    assert Path(str(preview_source)) == motion_abs
    controller.activate_index(index)
    qapp.processEvents()

    assert media.loaded == motion_abs
    assert media.play_calls == 1
    assert player_stack.currentWidget() is video_area
    assert media._muted is True
    assert not player_bar.isEnabled()
    assert live_badge.isVisible()
    assert not video_area.player_bar.isVisible()
    assert status_bar.currentMessage().startswith("Playing Live Photo")

    controller.handle_media_status_changed(SimpleNamespace(name="EndOfMedia"))
    qapp.processEvents()

    assert media.stopped
    assert player_stack.currentWidget() is image_viewer
    assert status_bar.currentMessage().startswith("Viewing IMG_5001")
    assert not player_bar.isEnabled()
    assert live_badge.isVisible()

    controller.replay_live_photo()
    qapp.processEvents()

    assert media.play_calls == 2
    assert player_stack.currentWidget() is video_area
    assert media._muted is True
    assert live_badge.isVisible()

    controller.handle_media_status_changed(SimpleNamespace(name="EndOfMedia"))
    qapp.processEvents()
    assert live_badge.isVisible()

    image_viewer.replayRequested.emit()
    qapp.processEvents()
    assert media.play_calls == 3

def test_thumbnail_job_seek_targets_clamp(tmp_path: Path, qapp: QApplication) -> None:
    dummy_loader = cast(Any, object())
    video_path = tmp_path / "clip.MOV"
    video_path.touch()
    cache_path = tmp_path / "cache.png"
    job = ThumbnailJob(
        dummy_loader,
        "clip.MOV",
        video_path,
        QSize(192, 192),
        1,
        cache_path,
        is_image=False,
        is_video=True,
        still_image_time=0.2,
        duration=0.06,
    )
    targets = job._seek_targets()
    assert targets[0] == pytest.approx(0.03, rel=1e-3)
    assert targets[1:] == [None]


def test_thumbnail_job_seek_targets_without_hint(tmp_path: Path, qapp: QApplication) -> None:
    dummy_loader = cast(Any, object())
    video_path = tmp_path / "clip.MOV"
    video_path.touch()
    cache_path = tmp_path / "cache.png"
    job = ThumbnailJob(
        dummy_loader,
        "clip.MOV",
        video_path,
        QSize(192, 192),
        1,
        cache_path,
        is_image=False,
        is_video=True,
        still_image_time=None,
        duration=None,
    )
    targets = job._seek_targets()
    assert targets == [None]

    with_duration = ThumbnailJob(
        dummy_loader,
        "clip.MOV",
        video_path,
        QSize(192, 192),
        1,
        cache_path,
        is_image=False,
        is_video=True,
        still_image_time=None,
        duration=4.0,
    )
    duration_targets = with_duration._seek_targets()
    assert duration_targets[0] == pytest.approx(2.0, rel=1e-3)
    assert duration_targets[1:] == [None]
