"""Floating tooltip widget that sidesteps ``QToolTip`` transparency issues."""

from __future__ import annotations

from typing import Iterable, Set, cast

from PySide6.QtCore import QObject, QEvent, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPalette,
    QHelpEvent,
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
    """Top-level tooltip widget that performs its own painting.

    The standard ``QToolTip`` inherits ``WA_TranslucentBackground`` from the
    frameless main window, forcing the platform to composite the popup without
    Qt ever drawing an opaque background.  On some window managers this yields a
    solid black rectangle.  By taking over the painting inside a dedicated
    ``QWidget`` we can always draw an opaque backdrop and sidestep those
    platform quirks entirely.
    """

    _CURSOR_OFFSET = QPoint(14, 22)
    _MAX_WIDTH = 340

    def __init__(self, parent: QWidget | None = None) -> None:
        # The tooltip is created as a stand-alone window so it can freely float
        # above the map without stealing focus or activating the parent.  Qt
        # still accepts a *parent* argument which keeps the object lifetime tied
        # to that owner without altering the toplevel behaviour that ``Qt.Tool``
        # provides for the widget itself.
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )

        # ``WA_TranslucentBackground`` must be explicitly disabled so the
        # tooltip paints onto an opaque surface.  Windows in a frameless,
        # translucent hierarchy inherit transparency by default which is the
        # root cause of the black rectangles we observed.  ``WA_OpaquePaintEvent``
        # and ``WA_NoSystemBackground`` instruct Qt to skip any platform
        # back-fill and trust our :meth:`paintEvent` to draw every pixel.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        # ``WA_ShowWithoutActivating`` allows the tooltip to appear even if the
        # application is not the active window.  ``WA_TransparentForMouseEvents``
        # ensures the popup never intercepts clicks, keeping interactions with
        # map markers responsive.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # Text and layout configuration used during painting.
        self._text: str = ""
        # Prefer the platform's tooltip font when available so the helper
        # mirrors native widgets.  ``QGuiApplication.font("QToolTip")`` falls
        # back to the default application font when the platform has no
        # distinct tooltip face configured.
        tooltip_font = QGuiApplication.font("QToolTip")
        self._font = QFont(tooltip_font)
        if self._font.pointSize() > 0:
            self._font.setPointSize(self._font.pointSize())
        self._padding: int = 6

        # Resolve palette aware colours while forcing the alpha channel to be
        # fully opaque so the tooltip never inherits translucent shades from the
        # parent window shell.  The fallbacks intentionally lean towards the
        # pale yellow and soft grey tones that Windows tooltips use so the
        # custom popup still feels familiar when the palette omits dedicated
        # tooltip roles.
        palette: QPalette = QGuiApplication.palette()
        self._background_color = self._opaque_colour(
            palette.color(QPalette.ColorRole.ToolTipBase), QColor(255, 255, 225)
        )
        self._text_color = self._opaque_colour(
            palette.color(QPalette.ColorRole.ToolTipText), QColor(Qt.GlobalColor.black)
        )
        self._border_color = self._opaque_colour(
            palette.color(QPalette.ColorRole.Mid), QColor("#999999")
        )
        self._corner_radius: float = 4.0
        self._border_width: int = 1

        self.hide()

    @staticmethod
    def _opaque_colour(candidate: QColor, fallback: QColor) -> QColor:
        """Return a fully opaque colour, defaulting to *fallback* when empty."""

        colour = QColor(candidate) if candidate.isValid() else QColor(fallback)
        if colour.alpha() != 255:
            colour.setAlpha(255)
        return colour

    def sizeHint(self) -> QSize:  # noqa: D401 - Qt docs describe the contract
        """Qt override: report the tooltip size required for the current text."""

        if not self._text:
            edge = 2 * (self._padding + self._border_width)
            return QSize(edge, edge)

        metrics = QFontMetrics(self._font)
        max_width = max(1, self._MAX_WIDTH - 2 * self._padding)
        text_rect = metrics.boundingRect(
            QRect(0, 0, max_width, 0),
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            self._text,
        )
        width = text_rect.width() + 2 * (self._padding + self._border_width)
        height = text_rect.height() + 2 * (self._padding + self._border_width)
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:  # noqa: D401 - mirrors :meth:`sizeHint`
        """Qt override: defer to :meth:`sizeHint` for layout calculations."""

        return self.sizeHint()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        """Qt override: render the rounded background and tooltip text."""

        del event  # The event is unused but included to satisfy Qt's signature.

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Clearing the canvas prevents residual pixels from previous frames
        # from bleeding through around the rounded corners when the tooltip is
        # resized to fit new text content.  This mirrors how native tooltips are
        # rendered on composited desktops.
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)

        # ``adjusted`` avoids clipping the border by shrinking the rect by half
        # the border width on each side.
        rect = QRectF(self.rect()).adjusted(
            0.5 * self._border_width,
            0.5 * self._border_width,
            -0.5 * self._border_width,
            -0.5 * self._border_width,
        )
        path = QPainterPath()
        path.addRoundedRect(rect, self._corner_radius, self._corner_radius)

        painter.fillPath(path, self._background_color)

        if self._border_width > 0:
            pen = painter.pen()
            pen.setColor(self._border_color)
            pen.setWidth(self._border_width)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(path)
        else:
            painter.setPen(Qt.PenStyle.NoPen)

        if self._text:
            painter.setPen(self._text_color)
            painter.setFont(self._font)
            text_rect = rect.adjusted(
                self._padding,
                self._padding,
                -self._padding,
                -self._padding,
            )
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft
                | Qt.AlignmentFlag.AlignVCenter
                | Qt.TextFlag.TextWordWrap,
                self._text,
            )

        painter.end()

    def show_text(self, global_pos: QPoint, text: str) -> None:
        """Display *text* near *global_pos* while keeping the popup on-screen."""

        if not text:
            self.hide_tooltip()
            return

        self._text = text
        self.resize(self.sizeHint())
        self.update()

        target = QPoint(global_pos)
        target += self._CURSOR_OFFSET
        geometry = QRect(target, self.size())

        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()

            if geometry.right() > available.right():
                geometry.moveRight(available.right())

            if geometry.bottom() > available.bottom():
                # Place the tooltip above the cursor while maintaining a clear
                # gap so the pointer does not obscure the marker.
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
        """Hide the popup and clear the cached text to avoid stale state."""

        if self.isVisible():
            self.hide()
        self._text = ""

    # ``MainWindow`` uses ``show_tooltip`` to mirror the ``QToolTip`` API.
    show_tooltip = show_text


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
