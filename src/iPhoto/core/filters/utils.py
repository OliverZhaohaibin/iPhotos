"""Platform-specific utilities for Qt image buffer handling.

This module provides utilities for working with QImage pixel buffers across
different Qt bindings (PySide vs PyQt), handling the subtle differences in
their memory view APIs.
"""

from __future__ import annotations

from PySide6.QtGui import QImage


def _resolve_pixel_buffer(image: QImage) -> tuple[memoryview, object]:
    """Return a writable 1-D :class:`memoryview` over *image*'s pixels.

    Qt offers subtly different behaviours across bindings when exposing the
    raw pixel buffer.  PyQt returns a ``sip.voidptr`` that requires an explicit
    ``setsize`` call before Python can view the memory, while PySide exposes a
    ready-to-use ``memoryview`` instance.  Some downstream forks even ship
    stripped variants that only implement part of either API.  The helper keeps
    the fast path for modern PySide builds while gracefully falling back to the
    more verbose PyQt sequence, all without relying on private sip internals.

    The tuple's second element ensures the underlying Qt buffer stays alive for
    as long as the view is in scope.  Losing that reference allows the garbage
    collector to reclaim the temporary wrapper, which would corrupt future
    writes when Python keeps using the now-dangling ``memoryview``.
    """

    bytes_per_line = image.bytesPerLine()
    height = image.height()
    buffer = image.bits()
    expected_size = bytes_per_line * height

    # Preserve a reference to the original object so its lifetime matches the
    # returned memoryview.  The type differs across bindings (PySide's
    # ``memoryview`` vs. PyQt's ``sip.voidptr``) which is why the helper returns
    # it in the tuple.
    guard: object = buffer

    if isinstance(buffer, memoryview):
        view = buffer
    else:
        # PyQt requires ``setsize`` to expose the buffer length.  Only call the
        # method when it exists to avoid repeating the PySide crash that stemmed
        # from invoking the non-existent attribute.
        try:
            view = memoryview(buffer)
        except TypeError:
            if hasattr(buffer, "setsize"):
                buffer.setsize(expected_size)
                view = memoryview(buffer)
            else:
                raise RuntimeError("Unsupported QImage.bits() buffer wrapper") from None

    # Normalise the layout to unsigned bytes so per-channel offsets are
    # consistent regardless of the binding.  ``cast`` already returns ``self``
    # when the format matches, so there is no extra allocation on the fast path.
    try:
        view = view.cast("B")
    except TypeError:
        # Python < 3.12 expects the shape argument when recasting a multi-
        # dimensional memoryview.  Using the total number of bytes keeps the API
        # compatible with older interpreters that still ship with some Qt
        # distributions.
        view = view.cast("B", (view.nbytes,))

    if len(view) < expected_size:
        # Some bindings expose padding that is smaller than ``bytesPerLine`` x
        # ``height``.  Restrict the view rather than risking out-of-bounds
        # writes.
        view = view[:expected_size]

    return view, guard
