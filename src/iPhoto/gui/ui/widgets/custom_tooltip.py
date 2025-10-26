"""Utilities for rendering an opaque tooltip on translucent window shells."""

from __future__ import annotations

import math
from typing import Iterable, Set, cast

from PySide6.QtCore import QObject, QEvent, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPalette,
    QHelpEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QWidget


_HIDE_EVENTS: Set[QEvent.Type] = {
    QEvent.Type.Leave,
    QEvent.Type.Hide,
    QEvent.Type.FocusOut,
    QEvent.Type.WindowDeactivate,
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.KeyPress,
    QEvent.Type.Close,
}


class FloatingToolTip(QWidget):
    """Top-level tooltip widget that performs its own opaque painting.

    The frameless main window enables ``WA_TranslucentBackground`` which causes
    ``QToolTip`` popups to inherit a transparent backing.  On Windows this often
    leaves the tooltip to be composited without ever drawing an opaque
    background, producing unreadable black rectangles.  ``FloatingToolTip``
    replaces the native helper with a dedicated ``QWidget`` whose paint routine
    first fills the rounded background and then overlays the border, ensuring
    every edge pixel blends against the tooltip's colours instead of the window
    manager's default backdrop.
    """

    _CURSOR_OFFSET = QPoint(14, 22)
    _MAX_WIDTH = 340

    def __init__(self, parent: QWidget | None = None) -> None:
        # ``Qt.Tool`` keeps the popup as an independent window while still
        # allowing the caller to parent it for lifetime management.  Combined
        # with ``FramelessWindowHint`` it produces a floating widget that never
        # steals focus from the rest of the application.
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )

        # Match the translucency behaviour of the frameless main window.  The
        # paint routine below renders an opaque backdrop manually, therefore the
        # widget can participate in alpha compositing without leaking black
        # halos along the rounded edges.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("floatingToolTip")

        self._padding = 6
        self._border_width = 1
        self._corner_radius = 6.0

        # Respect the palette whenever possible so the tooltip integrates with
        # the current theme while falling back to readable colours when the
        # palette omits dedicated tooltip roles (a common occurrence on Linux).
        palette = QGuiApplication.palette()
        self._background_colour = self._resolve_colour(
            palette.color(QPalette.ColorRole.ToolTipBase), QColor("#ffffe1")
        )
        self._text_colour = self._resolve_colour(
            palette.color(QPalette.ColorRole.ToolTipText), QColor("#000000")
        )
        self._border_colour = self._resolve_colour(
            palette.color(QPalette.ColorRole.Mid), QColor("#999999")
        )

        tooltip_font = QGuiApplication.font("QToolTip")
        self._font = QFont(tooltip_font)

        self._last_text: str = ""
        self.hide()

    @staticmethod
    def _resolve_colour(candidate: QColor, fallback: QColor) -> QColor:
        """Return an opaque colour derived from ``candidate`` or ``fallback``."""

        colour = QColor(candidate) if candidate.isValid() else QColor(fallback)
        if colour.alpha() != 255:
            colour.setAlpha(255)
        return colour

    def setText(self, text: str) -> None:
        """Update the tooltip content and recompute the preferred geometry."""

        normalised = text or ""
        if normalised == self._last_text:
            # Even when the text is unchanged the layout may require a refresh
            # after the widget was hidden, therefore ``adjustSize`` is still
            # invoked to keep the frame tightly wrapped around the painted text.
            self.adjustSize()
            self.update()
            return

        self._last_text = normalised
        self.adjustSize()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: D401 - Qt documents the contract
        """Qt override: compute the popup size for the current tooltip text."""

        if not self._last_text:
            edge = 2 * (self._padding + self._border_width)
            return QSize(edge, edge)

        metrics = QFontMetrics(self._font)
        available_width = max(0, self._MAX_WIDTH - 2 * self._padding)

        # ``QTextLayout`` reproduces the exact wrapping logic that
        # ``QPainter.drawText`` uses, but also exposes the natural width of each
        # rendered line.  This allows the tooltip to shrink to the longest actual
        # line instead of automatically stretching to the configured maximum
        # width, eliminating the excess whitespace that previously surrounded
        # short labels.
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        option.setAlignment(Qt.AlignmentFlag.AlignLeft)

        layout = QTextLayout(self._last_text, self._font)
        layout.setTextOption(option)

        layout.beginLayout()
        max_line_width = 0.0
        content_height = 0.0
        # ``setLineWidth`` cannot accept zero, therefore fall back to a tight
        # bound derived from the measured string width when wrapping is disabled
        # (``available_width`` evaluates to ``0`` for extremely small tooltips).
        line_width_constraint = float(
            available_width or max(1, metrics.horizontalAdvance(self._last_text) + 1)
        )
        line_count = 0

        while True:
            line = layout.createLine()
            if not line.isValid():
                break

            line.setLineWidth(line_width_constraint)

            # ``QTextLine.naturalTextWidth`` occasionally reports the assigned
            # wrapping width instead of the actual glyph advance, causing short
            # strings to expand to the maximum constraint.  Computing the
            # horizontal advance from the concrete slice of characters keeps the
            # tooltip width pinned to the longest rendered line regardless of
            # how ``QTextLayout`` internally normalises the measurement.
            start = line.textStart()
            length = line.textLength()
            line_text = self._last_text[start : start + length]

            # ``QTextLayout`` keeps explicit newline characters at the end of
            # a line.  They do not contribute to the painted width, therefore we
            # strip them before querying the metrics while leaving any other
            # leading or trailing whitespace untouched.
            stripped_line = line_text.rstrip("\n\r")
            measured_line = stripped_line or " "

            max_line_width = max(
                max_line_width, float(metrics.horizontalAdvance(measured_line))
            )
            content_height += line.height()
            line_count += 1

        layout.endLayout()

        if line_count == 0:
            # ``QTextLayout`` creates zero lines for an empty string.  Fall back
            # to the font metrics so the tooltip still reserves a minimal frame
            # for whitespace-only tooltips.
            max_line_width = float(metrics.horizontalAdvance(self._last_text.strip() or " "))
            content_height = float(metrics.height())

        if available_width:
            # ``naturalTextWidth`` may overshoot the assigned width by a small
            # floating-point margin.  Clamping keeps the tooltip within the
            # user-configured width budget while still fitting tightly around the
            # measured line lengths.
            max_line_width = min(max_line_width, float(available_width))

        # Guarantee a sensible fallback for purely whitespace content.  Without
        # this guard the tooltip could collapse to zero width, making the border
        # invisible despite the text technically containing characters.
        fallback_width = float(metrics.horizontalAdvance(self._last_text.strip() or " "))
        content_width = max(max_line_width, fallback_width)

        frame_inset = 2 * (self._padding + self._border_width)
        width = math.ceil(content_width) + frame_inset
        height = math.ceil(content_height) + frame_inset

        min_edge = frame_inset
        width = max(width, min_edge)
        height = max(height, min_edge)
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:  # noqa: D401 - mirrors :meth:`sizeHint`
        """Qt override: defer to :meth:`sizeHint` for layout calculations."""

        return self.sizeHint()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        """Draw a clipped, rounded rectangle with the tooltip text."""

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        outer_rect = QRectF(self.rect())
        # The outer radius matches the requested corner rounding but is clamped
        # so extremely small frames do not attempt to draw impossible curves.
        outer_radius = min(
            self._corner_radius, outer_rect.width() / 2.0, outer_rect.height() / 2.0
        )

        # Step 1: paint the full rounded rectangle with the background colour.
        # Drawing the fill first ensures any anti-aliased edge pixels blend with
        # the tooltip's own colour rather than the compositor's fallback shade,
        # preventing black halos on translucent parent windows.
        paint_rect = outer_rect.adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(self._corner_radius, paint_rect.width() / 2.0, paint_rect.height() / 2.0)
        rounded_path = QPainterPath()
        rounded_path.addRoundedRect(paint_rect, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._background_colour)
        painter.drawPath(rounded_path)

        # Step 2: stroke the same path using the configured border colour.  The
        # stroke overlays the background fill, producing a crisp outline without
        # exposing semi-transparent edge pixels to the window manager.
        if self._border_width > 0 and self._border_colour.alpha() > 0:
            border_pen = QPen(self._border_colour)
            border_pen.setWidthF(self._border_width)
            border_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(border_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(rounded_path)

        if self._last_text:
            painter.setFont(self._font)
            painter.setPen(self._text_colour)
            text_inset = self._padding + self._border_width
            text_rect = QRectF(paint_rect).adjusted(
                text_inset,
                text_inset,
                -text_inset,
                -text_inset,
            )
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft
                | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap,
                self._last_text,
            )

        painter.end()

    def show_tooltip(self, global_pos: QPoint, text: str) -> None:
        """Display *text* near *global_pos* while keeping the popup on screen."""

        if not text:
            self.hide_tooltip()
            return

        self.setText(text)
        tooltip_size = self.sizeHint()
        self.resize(tooltip_size)

        target = QPoint(global_pos)
        target += self._CURSOR_OFFSET
        geometry = QRect(target, tooltip_size)

        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()

            if geometry.right() > available.right():
                geometry.moveRight(global_pos.x() - self._CURSOR_OFFSET.x())

            if geometry.bottom() > available.bottom():
                geometry.moveBottom(global_pos.y() - self._CURSOR_OFFSET.y())

            if geometry.left() < available.left():
                geometry.moveLeft(available.left())

            if geometry.top() < available.top():
                geometry.moveTop(available.top())

        self.setGeometry(geometry)
        if not self.isVisible():
            self.show()
        self.raise_()

    def hide_tooltip(self) -> None:
        """Hide the popup without discarding the cached tooltip text."""

        if self.isVisible():
            self.hide()

    # ``MainWindow`` and ``PhotoMapView`` mirror the ``QToolTip`` API by using
    # ``show_tooltip``.  Retain a ``show_text`` alias so older call sites remain
    # compatible with the helper.
    show_text = show_tooltip


class ToolTipEventFilter(QObject):
    """Event filter that reroutes ``QToolTip`` events to :class:`FloatingToolTip`."""

    def __init__(self, tooltip: FloatingToolTip, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tooltip = tooltip
        # Track objects that should bypass the filter entirely.  The tooltip
        # widget itself must be ignored or Qt will immediately re-enter the
        # filter when it receives synthetic events such as ``Leave`` while
        # hiding the popup.
        self._ignored_ids: Set[int] = {id(tooltip)}

    def ignore_object(self, obj: QObject) -> None:
        """Exclude *obj* from tooltip interception logic."""

        self._ignored_ids.add(id(obj))

    def ignore_many(self, objects: Iterable[QObject]) -> None:
        """Convenience helper to add multiple ignored objects in one call."""

        for obj in objects:
            self.ignore_object(obj)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        """Intercept tooltip events and display them using the floating popup."""

        if id(watched) in self._ignored_ids:
            return False

        event_type = event.type()
        if event_type == QEvent.Type.ToolTip:
            help_event = cast(QHelpEvent, event)

            # ``QHelpEvent`` gained ``text()`` in newer Qt releases, however
            # several PySide6 builds – including the version bundled with the
            # project – omit the accessor.  Query the attribute defensively so
            # the event filter remains compatible with runtimes that expose the
            # data exclusively through ``QWidget.toolTip``.
            text_getter = getattr(help_event, "text", None)
            text = text_getter() if callable(text_getter) else None

            if not text:
                # Some widgets populate the help event without copying the
                # tooltip string.  Falling back to ``QWidget.toolTip`` mimics
                # Qt's default behaviour so the popup always receives the
                # expected copy.
                tooltip_attr = getattr(watched, "toolTip", None)
                if callable(tooltip_attr):
                    text = tooltip_attr()

            text = text.strip() if text else ""
            if text:
                self._tooltip.show_tooltip(help_event.globalPos(), text)
            else:
                self._tooltip.hide_tooltip()
            # Returning ``True`` prevents Qt from spawning the native tooltip,
            # ensuring the floating helper is the only popup that appears.
            return True

        if event_type in _HIDE_EVENTS or event_type == QEvent.Type.Destroy:
            # Events that naturally conclude tooltip interactions (for example
            # pressing a mouse button or hiding the source widget) must dismiss
            # the floating popup to mirror Qt's native behaviour.  Returning
            # ``False`` allows the original widget to continue processing the
            # event normally.
            self._tooltip.hide_tooltip()
            return False

        return False


__all__ = ["FloatingToolTip", "ToolTipEventFilter"]
