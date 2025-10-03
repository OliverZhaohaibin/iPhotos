"""Tests for the lightweight ffmpeg helpers."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from iPhotos.src.iPhoto.utils import ffmpeg
from iPhotos.src.iPhoto.errors import ExternalToolError


def _fake_completed_process(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")


def test_extract_video_frame_uses_yuv_format_for_jpeg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ensure JPEG extractions request a YUV pixel format."""

    input_path = tmp_path / "movie.mp4"
    input_path.touch()
    captured: dict[str, list[str]] = {}

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        captured["cmd"] = command
        Path(command[-1]).write_bytes(b"jpeg")
        return _fake_completed_process(command)

    monkeypatch.setattr(ffmpeg, "_run_command", fake_run)

    data = ffmpeg.extract_video_frame(input_path, at=0.5, scale=(320, 240), format="jpeg")

    assert data == b"jpeg"
    assert "cmd" in captured
    command = captured["cmd"]
    assert "-vf" in command
    vf_index = command.index("-vf")
    vf_expression = command[vf_index + 1]
    assert "format=yuv420p" in vf_expression
    assert "scale=min(320,iw):min(240,ih):force_original_aspect_ratio=decrease" in vf_expression
    assert "scale=max(2,trunc(iw/2)*2):max(2,trunc(ih/2)*2)" in vf_expression
    assert "format=rgba" not in vf_expression


def test_extract_video_frame_uses_rgba_for_png(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PNG extractions keep the alpha channel when available."""

    input_path = tmp_path / "movie.mov"
    input_path.touch()
    captured: dict[str, list[str]] = {}

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        captured["cmd"] = command
        Path(command[-1]).write_bytes(b"png")
        return _fake_completed_process(command)

    monkeypatch.setattr(ffmpeg, "_run_command", fake_run)

    data = ffmpeg.extract_video_frame(input_path, at=None, scale=None, format="png")

    assert data == b"png"
    assert "cmd" in captured
    command = captured["cmd"]
    assert "-vf" in command
    vf_index = command.index("-vf")
    vf_expression = command[vf_index + 1]
    assert "format=rgba" in vf_expression
    assert "format=yuv420p" not in vf_expression
    assert "scale=max(2,trunc(iw/2)*2):max(2,trunc(ih/2)*2)" not in vf_expression


def test_extract_video_frame_without_scale_enforces_even_dimensions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """JPEG requests without scaling still force even-sized frames."""

    input_path = tmp_path / "clip.mp4"
    input_path.touch()
    captured: dict[str, list[str]] = {}

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        captured["cmd"] = command
        Path(command[-1]).write_bytes(b"jpeg")
        return _fake_completed_process(command)

    monkeypatch.setattr(ffmpeg, "_run_command", fake_run)

    data = ffmpeg.extract_video_frame(input_path, at=None, scale=None, format="jpeg")

    assert data == b"jpeg"
    assert "cmd" in captured
    command = captured["cmd"]
    assert "-vf" in command
    vf_index = command.index("-vf")
    vf_expression = command[vf_index + 1]
    assert "scale=iw:ih" in vf_expression
    assert "scale=max(2,trunc(iw/2)*2):max(2,trunc(ih/2)*2)" in vf_expression


def test_extract_video_frame_falls_back_to_opencv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When ffmpeg fails, OpenCV fallback results are returned if available."""

    input_path = tmp_path / "clip.mov"
    input_path.touch()

    def fake_ffmpeg(*args: object, **kwargs: object) -> bytes:
        raise ExternalToolError("boom")

    fallback_data = b"opencv"

    def fake_opencv(*args: object, **kwargs: object) -> bytes:
        return fallback_data

    monkeypatch.setattr(ffmpeg, "_extract_with_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(ffmpeg, "_extract_with_opencv", fake_opencv)

    data = ffmpeg.extract_video_frame(input_path, at=0.1, scale=(100, 100), format="jpeg")

    assert data is fallback_data


def test_extract_video_frame_propagates_error_when_no_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without OpenCV, the original ffmpeg error is raised."""

    input_path = tmp_path / "clip.mov"
    input_path.touch()

    def fake_ffmpeg(*args: object, **kwargs: object) -> bytes:
        raise ExternalToolError("missing tool")

    monkeypatch.setattr(ffmpeg, "_extract_with_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(ffmpeg, "_extract_with_opencv", lambda *args, **kwargs: None)

    with pytest.raises(ExternalToolError):
        ffmpeg.extract_video_frame(input_path, format="jpeg")


def test_extract_with_opencv_scales_and_encodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The OpenCV helper rescales frames and encodes JPEG output."""

    input_path = tmp_path / "video.mp4"
    input_path.touch()

    class FakeBuffer:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def __bytes__(self) -> bytes:
            return self._data

        def tobytes(self) -> bytes:  # pragma: no cover - compatibility shim
            return self._data

    class FakeFrame:
        def __init__(self, width: int, height: int) -> None:
            self.shape = (height, width, 3)
            self.width = width
            self.height = height

    class FakeCapture:
        def __init__(self) -> None:
            self.set_calls: list[tuple[int, float]] = []
            self.released = False

        def isOpened(self) -> bool:
            return True

        def set(self, prop: int, value: float) -> bool:
            self.set_calls.append((prop, value))
            return True

        def get(self, prop: int) -> float:
            return 24.0 if prop == 1 else 0.0

        def read(self) -> tuple[bool, FakeFrame]:
            return True, FakeFrame(7, 5)

        def release(self) -> None:
            self.released = True

    capture = FakeCapture()
    resize_calls: list[tuple[int, int]] = []
    encode_calls: list[tuple[str, tuple[int, int], list[int]]] = []

    class FakeCV2:
        CAP_PROP_POS_MSEC = 0
        CAP_PROP_FPS = 1
        CAP_PROP_POS_FRAMES = 2
        INTER_AREA = 3
        IMWRITE_JPEG_QUALITY = 4

        @staticmethod
        def VideoCapture(path: str) -> FakeCapture:  # type: ignore[override]
            assert path == str(input_path)
            return capture

        @staticmethod
        def resize(frame: FakeFrame, size: tuple[int, int], interpolation: int) -> FakeFrame:
            resize_calls.append(size)
            return FakeFrame(size[0], size[1])

        @staticmethod
        def imencode(ext: str, frame: FakeFrame, params: list[int]) -> tuple[bool, FakeBuffer]:
            encode_calls.append((ext, frame.shape[:2], params))
            return True, FakeBuffer(b"encoded")

    monkeypatch.setattr(ffmpeg, "cv2", FakeCV2)

    data = ffmpeg._extract_with_opencv(input_path, at=0.5, scale=(4, 4), format="jpeg")

    assert data == b"encoded"
    assert len(resize_calls) == 1
    resized_width, resized_height = resize_calls[0]
    assert resized_width <= 4 and resized_height <= 4
    assert resized_width % 2 == 0 and resized_height % 2 == 0
    assert encode_calls == [
        (".jpg", (resized_height, resized_width), [FakeCV2.IMWRITE_JPEG_QUALITY, 92])
    ]
    assert capture.set_calls[0][0] == FakeCV2.CAP_PROP_POS_MSEC
    assert capture.released is True
