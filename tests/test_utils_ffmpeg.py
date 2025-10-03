"""Tests for the lightweight ffmpeg helpers."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from iPhotos.src.iPhoto.utils import ffmpeg


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
