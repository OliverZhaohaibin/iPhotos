"""Tests for the pydevd tracing compatibility shim."""

from __future__ import annotations

import importlib
import sys


def test_pydevd_tracing_stub_forces_fallback(monkeypatch):
    monkeypatch.setenv("IPHOTO_FORCE_CTYPES_STUB", "1")
    sys.modules.pop("pydevd_tracing", None)
    module = importlib.import_module("pydevd_tracing")
    try:
        assert hasattr(module, "SetTrace")
        module.SetTrace(None)
        module.reapply_settrace()
        assert module.set_trace_to_threads(lambda *_: None) == -1
    finally:
        sys.modules.pop("pydevd_tracing", None)
