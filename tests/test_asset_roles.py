from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for GUI tests", exc_type=ImportError)
pytest.importorskip("PySide6.QtWidgets", reason="Qt widgets not available", exc_type=ImportError)

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from iPhotos.src.iPhoto.gui.facade import AppFacade
from iPhotos.src.iPhoto.gui.ui.models.asset_model import AssetModel, Roles

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover - pillow missing or broken
    pytest.skip(
        f"Pillow unavailable for asset role tests: {exc}",
        allow_module_level=True,
    )


def _create_image(path: Path) -> None:
    image = Image.new("RGB", (10, 10), color="green")
    image.save(path)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_asset_roles_expose_metadata(tmp_path: Path, qapp: QApplication) -> None:
    still = tmp_path / "IMG_0001.JPG"
    _create_image(still)
    video = tmp_path / "CLIP_0001.MP4"
    video.write_bytes(b"")

    facade = AppFacade()
    model = AssetModel(facade)

    # ``AssetListModel`` performs I/O in a background thread, therefore the
    # model will report ``rowCount == 0`` until the worker announces completion.
    # ``QSignalSpy`` gives the test an explicit synchronisation point so we only
    # inspect the model after ``loadFinished`` indicates that rows are ready.
    load_spy = QSignalSpy(facade.loadFinished)

    facade.open_album(tmp_path)

    if not load_spy.wait(5000):
        pytest.fail("Timed out waiting for the asset list to finish loading")

    # ``QSignalSpy`` stores the captured emissions as a list of argument lists,
    # but the spy itself is not subscriptable.  Query ``count`` first to make the
    # following assertions explicit and then inspect the first emission via
    # :meth:`at`.  PySide6 does not implement ``first`` on the spy object, so
    # ``at(0)`` is the supported way to read the first payload without removing
    # it from the queue.  This also prevents ``TypeError`` when the spy has no
    # recorded entries yet.
    assert load_spy.count() == 1
    load_finished_args = load_spy.at(0)
    assert len(load_finished_args) == 2

    # The ``loadFinished`` signal emits ``(album_root: Path, success: bool)``;
    # validate both so the test fails loudly if the background loader reports an
    # error for the temporary album used in this fixture.
    album_root_from_signal, success_flag = load_finished_args
    assert isinstance(album_root_from_signal, Path)
    assert album_root_from_signal.resolve() == tmp_path.resolve()
    assert success_flag is True

    qapp.processEvents()

    assert model.rowCount() == 2
    rows = [model.index(row, 0) for row in range(model.rowCount())]

    rels = {index.data(Roles.REL) for index in rows}
    assert rels == {"IMG_0001.JPG", "CLIP_0001.MP4"}

    for index in rows:
        rel = index.data(Roles.REL)
        abs_path = Path(index.data(Roles.ABS))
        assert abs_path == (tmp_path / rel).resolve()
        if rel.endswith("JPG"):
            assert index.data(Roles.IS_IMAGE) is True
            assert index.data(Roles.IS_VIDEO) is False
        else:
            assert index.data(Roles.IS_VIDEO) is True
            assert index.data(Roles.IS_IMAGE) is False

    # Mark the still as featured and ensure the role updates after reload.
    assert facade.current_album is not None
    facade.current_album.manifest["featured"] = ["IMG_0001.JPG"]
    # ``AssetListModel.update_featured_status`` is the supported entry point for
    # synchronising the UI with manifest changes.  Emitting ``indexUpdated``
    # only informs listeners that a refresh already happened, so the model would
    # otherwise keep stale data.  Updating the source model directly mirrors the
    # behaviour performed by :meth:`AppFacade.toggle_featured` in production.
    model.source_model().update_featured_status("IMG_0001.JPG", True)
    qapp.processEvents()

    featured_index = next(
        model.index(row, 0)
        for row in range(model.rowCount())
        if model.data(model.index(row, 0), Roles.REL) == "IMG_0001.JPG"
    )
    assert featured_index.data(Roles.FEATURED) is True

    model.set_filter_mode("favorites")
    qapp.processEvents()
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), Roles.REL) == "IMG_0001.JPG"
