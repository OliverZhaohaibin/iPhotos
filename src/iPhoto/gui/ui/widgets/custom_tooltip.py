"""Utilities for rendering an opaque tooltip on translucent window shells."""

from __future__ import annotations

from math import ceil
from typing import Iterable, Set, cast

from PySide6.QtCore import QObject, QEvent, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QTextLayout,
    QTextOption,
    QPalette,
    QHelpEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
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

        edge = 2 * (self._padding + self._border_width)
        if not self._last_text:
            return QSize(edge, edge)

        metrics = QFontMetrics(self._font)
        # ``available_text_width`` describes the space that remains for text once
        # padding and borders are removed.  The ``max`` call keeps the value
        # non-negative so later calculations do not have to deal with unexpected
        # negative limits when the caller configures an unusually small
        # ``_MAX_WIDTH``.
        available_text_width = max(0, self._MAX_WIDTH - 2 * self._padding)

        # ``QTextLayout`` exposes the exact metrics that Qt uses when painting
        # wrapped paragraphs.  By iterating over every generated line we can
        # capture the *actual* rendered width instead of relying on
        # ``boundingRect`` which frequently stretches multi-word strings to the
        # maximum width constraint.
        layout = QTextLayout(self._last_text, self._font)
        layout_option = QTextOption()
        layout_option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout_option.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.setTextOption(layout_option)

        # Qt treats ``lineWidth`` values of zero as "wrap anywhere"; providing a
        # large positive sentinel therefore disables wrapping whenever the caller
        # does not impose a width constraint.  ``1_000_000`` comfortably exceeds
        # any reasonable tooltip width while keeping floating-point rounding in
        # check.
        line_width_constraint = float(available_text_width) if available_text_width > 0 else 1_000_000.0

        layout.beginLayout()
        max_line_width = 0.0
        total_height = 0.0
        line_count = 0

        while True:
            line = layout.createLine()
            if not line.isValid():
                break

            line.setLineWidth(line_width_constraint)
            max_line_width = max(max_line_width, line.naturalTextWidth())
            total_height += line.height()
            line_count += 1

        layout.endLayout()

        # ``QTextLayout`` may return zero lines for strings that only consist of
        # newline characters.  Fall back to the standard font metrics so the
        # tooltip still reserves a legible frame.
        if line_count == 0:
            max_line_width = metrics.horizontalAdvance(self._last_text)
            total_height = metrics.height()

        # When the text contains nothing but whitespace Qt reports a zero width
        # even though the user expects the tooltip to remain visible.  Measuring
        # a single space ensures the popup never collapses entirely.
        if self._last_text.strip() == "":
            max_line_width = max(max_line_width, metrics.horizontalAdvance(" "))

        # ``total_height`` can be marginally smaller than a single baseline when
        # the layout only produced spacing information.  Guarantee at least one
        # line of height so the surrounding frame always encloses a readable
        # region.
        total_height = max(total_height, metrics.height())

        width = ceil(max_line_width) + edge
        height = ceil(total_height) + edge

        # Clamp the tooltip width to ``_MAX_WIDTH`` to avoid exceeding the caller
        # supplied limit once padding and borders have been applied.
        width = min(max(width, edge), self._MAX_WIDTH)
        height = max(height, edge)
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
