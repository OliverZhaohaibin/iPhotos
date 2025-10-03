from iPhoto.utils.deps import debugger_prerequisites


def test_debugger_prerequisites_detects_ctypes_support():
    info = debugger_prerequisites()
    assert info.has_ctypes is True
    assert info.message is None
