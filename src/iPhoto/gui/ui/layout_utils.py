"""Utility helpers that preserve widget heights while edit chrome animates in and out."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

__all__ = [
    "ensure_default_vertical_constraints",
    "hide_collapsed_widget",
    "show_with_restored_height",
]

# Qt uses dynamic properties extensively, so we follow the same convention to keep the helpers
# lightweight.  These property keys are intentionally verbose so it is obvious what the cached
# values represent when inspecting the widget tree in a debugger.
_DEFAULT_MINIMUM_HEIGHT_PROP = "defaultMinimumHeight"
_DEFAULT_MAXIMUM_HEIGHT_PROP = "defaultMaximumHeight"
_DEFAULT_PREFERRED_HEIGHT_PROP = "defaultPreferredHeight"


def ensure_default_vertical_constraints(widget: QWidget) -> None:
    """Capture the widget's baseline vertical constraints as dynamic properties.

    Calling :func:`hide_collapsed_widget` overwrites the minimum and maximum height values with
    zero so the surrounding layout can fully reclaim the space while the widget is hidden.  Qt does
    not remember the previous constraints automatically, therefore we cache the original values the
    first time the helper is invoked.  Subsequent calls are inexpensive because the properties are
    only populated once.
    """

    if widget.property(_DEFAULT_MINIMUM_HEIGHT_PROP) is None:
        widget.setProperty(_DEFAULT_MINIMUM_HEIGHT_PROP, int(widget.minimumHeight()))
    if widget.property(_DEFAULT_MAXIMUM_HEIGHT_PROP) is None:
        widget.setProperty(_DEFAULT_MAXIMUM_HEIGHT_PROP, int(widget.maximumHeight()))
    if widget.property(_DEFAULT_PREFERRED_HEIGHT_PROP) is None:
        hint = max(widget.sizeHint().height(), widget.minimumHeight())
        widget.setProperty(_DEFAULT_PREFERRED_HEIGHT_PROP, int(hint))


def hide_collapsed_widget(widget: QWidget) -> None:
    """Collapse *widget* vertically and hide it without leaving phantom layout spacing.

    The helper ensures the caller can repeatedly hide the same widget without re-computing its
    baseline constraints.  The geometry update is important because Qt may otherwise keep the
    previous height cached until the next layout pass, causing visual jumps when animations start.
    """

    ensure_default_vertical_constraints(widget)
    widget.setMinimumHeight(0)
    widget.setMaximumHeight(0)
    widget.hide()
    widget.updateGeometry()


def show_with_restored_height(widget: QWidget) -> None:
    """Restore the widget's cached vertical constraints before showing it again.

    Qt defaults the maximum height of most containers to ``QWIDGETSIZE_MAX`` (``16777215``), but
    some widgets override the bound to match their size hint.  By reading back the cached values we
    respect the designer's intent regardless of which branch created the widget.  The helper also
    guards against degenerate cases where the cached numbers are missing or zero by falling back to
    the current size hint.  Finally, a geometry update nudges Qt to honour the fresh constraints
    immediately instead of waiting for the next event loop iteration.
    """

    ensure_default_vertical_constraints(widget)

    raw_min = widget.property(_DEFAULT_MINIMUM_HEIGHT_PROP)
    raw_max = widget.property(_DEFAULT_MAXIMUM_HEIGHT_PROP)
    raw_pref = widget.property(_DEFAULT_PREFERRED_HEIGHT_PROP)

    try:
        min_height = int(raw_min) if raw_min is not None else widget.minimumHeight()
    except (TypeError, ValueError):
        min_height = widget.minimumHeight()

    try:
        max_height = int(raw_max) if raw_max is not None else widget.maximumHeight()
    except (TypeError, ValueError):
        max_height = widget.maximumHeight()

    try:
        preferred = int(raw_pref) if raw_pref is not None else widget.sizeHint().height()
    except (TypeError, ValueError):
        preferred = widget.sizeHint().height()

    preferred = max(preferred, min_height)
    min_height = max(0, min_height)
    max_height = max(max_height, preferred)

    widget.setMinimumHeight(min_height)
    widget.setMaximumHeight(max_height)
    widget.show()
    widget.updateGeometry()
