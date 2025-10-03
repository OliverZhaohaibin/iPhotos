"""Compatibility shim that keeps PyCharm's debugger working without _ctypes.

This module mirrors the public surface of ``pydevd_tracing`` but degrades to a
pure Python implementation when the standard library ``_ctypes`` extension is
unavailable. Some Windows Python builds – especially lightweight Conda
installations – omit ``_ctypes`` which causes PyCharm's helper script
``pydevd.py`` to fail before user code runs. By shadowing the helper module we
can keep debugging available, albeit without the native fast-path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Iterable

_THIS_FILE = Path(__file__).resolve()
_THIS_DIR = _THIS_FILE.parent
_FORCE_STUB = os.environ.get("IPHOTO_FORCE_CTYPES_STUB") == "1"
_CTYPES_AVAILABLE = importlib.util.find_spec("_ctypes") is not None and not _FORCE_STUB


def _iter_search_roots() -> Iterable[Path]:
    helpers = os.environ.get("PYCHARM_HELPERS_DIR")
    if helpers:
        yield Path(helpers) / "pydev"
    for entry in sys.path:
        if not entry:
            continue
        path = Path(entry).resolve()
        if path == _THIS_DIR:
            continue
        yield path


def _load_upstream_module() -> ModuleType | None:
    for root in _iter_search_roots():
        candidate = root / "pydevd_tracing.py"
        if not candidate.exists():
            continue
        if candidate.resolve() == _THIS_FILE:
            continue
        spec = importlib.util.spec_from_file_location("_pydevd_tracing_upstream", candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except ImportError as exc:  # pragma: no cover - upstream missing optional deps
            if getattr(exc, "name", None) == "_ctypes":
                continue
            raise
        return module
    return None


if _CTYPES_AVAILABLE:
    _UPSTREAM = _load_upstream_module()
else:
    _UPSTREAM = None

if _UPSTREAM is not None:
    for _name, _value in _UPSTREAM.__dict__.items():
        if _name in {"__name__", "__loader__", "__spec__", "__package__", "__file__"}:
            continue
        globals()[_name] = _value
    sys.modules[__name__] = _UPSTREAM
else:
    _ORIGINAL_SETTRACE = sys.settrace

    class TracingFunctionHolder:
        """Matches the structure expected by ``pydevd``."""

        _original_tracing = None
        _warn = False
        _traceback_limit = 1
        _warnings_shown: dict[str, int] = {}
        _last_tracing = threading.local()

    __all__ = [
        "TracingFunctionHolder",
        "SetTrace",
        "set_trace_to_threads",
        "replace_sys_set_trace_func",
        "restore_sys_set_trace_func",
        "reapply_settrace",
        "stoptrace",
        "get_exception_traceback_str",
        "set_trace_for_frame_and_parents",
    ]

    _STUB_NOTICE_EMITTED = False

    def _emit_stub_notice() -> None:
        global _STUB_NOTICE_EMITTED
        if _STUB_NOTICE_EMITTED:
            return
        _STUB_NOTICE_EMITTED = True
        sys.stderr.write(
            "PyCharm debugger is running without _ctypes; falling back to a "
            "pure Python tracer. Native thread tracing and performance may "
            "be reduced.\n"
        )
        sys.stderr.flush()

    _emit_stub_notice()

    def get_exception_traceback_str() -> str:
        exc_type, exc_value, exc_tb = sys.exc_info()
        buffer = StringIO()
        traceback.print_exception(exc_type, exc_value, exc_tb, file=buffer)
        return buffer.getvalue()

    def replace_sys_set_trace_func() -> None:
        if TracingFunctionHolder._original_tracing is None:
            TracingFunctionHolder._original_tracing = sys.settrace

    def restore_sys_set_trace_func() -> None:
        new_value = TracingFunctionHolder._original_tracing or _ORIGINAL_SETTRACE
        sys.settrace(new_value)

    def SetTrace(tracing_func):  # noqa: N802 - API mirrors pydevd
        TracingFunctionHolder._last_tracing.tracing_func = tracing_func
        sys.settrace(tracing_func)
        threading.settrace(tracing_func)

    def reapply_settrace() -> None:
        try:
            tracing_func = TracingFunctionHolder._last_tracing.tracing_func
        except AttributeError:
            return
        sys.settrace(tracing_func)

    def set_trace_to_threads(tracing_func, thread_idents=None, create_dummy_thread=True):
        return -1

    def stoptrace() -> None:
        SetTrace(None)

    def set_trace_for_frame_and_parents(frame, trace_func) -> None:
        while frame is not None:
            frame.f_trace = trace_func
            frame = frame.f_back

    @dataclass
    class ThreadTracer:
        tracing_func: object

        def __call__(self, frame, event, arg):
            return self.tracing_func

    def thread_trace_dispatch(frame, event, arg):  # pragma: no cover - debugging helper
        try:
            tracing_func = TracingFunctionHolder._last_tracing.tracing_func
        except AttributeError:
            return None
        return tracing_func

    def make_thread_local_trace_dispatcher():  # pragma: no cover - API compatibility
        return thread_trace_dispatch

    globals().update(
        {
            "thread_trace_dispatch": thread_trace_dispatch,
            "make_thread_local_trace_dispatcher": make_thread_local_trace_dispatcher,
        }
    )

