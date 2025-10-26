"""Floating tooltip widget that sidesteps ``QToolTip`` transparency issues."""

from __future__ import annotations

from typing import Iterable, Set, cast

from PySide6.QtCore import QObject, QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPalette, QHelpEvent
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


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


class FloatingToolTip(QFrame):
    """Top-level tooltip widget rendered with Qt's style engine.

    The frameless main window enables ``WA_TranslucentBackground`` which causes
    ``QToolTip`` popups to inherit a transparent backing.  On Windows this often
    leaves the tooltip to be composited without ever drawing an opaque
    background, producing unreadable black rectangles.  ``FloatingToolTip``
    replaces the native helper with a dedicated ``QFrame`` that uses standard
    Qt styling rules, guaranteeing that the palette-derived colours and rounded
    corners are painted opaquely on every platform.
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

        # Opt out of the translucency inherited from the frameless main window
        # so the style engine is free to paint an opaque background.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("floatingToolTip")

        # ``QLabel`` handles text layout, including word wrapping.  The
        # container is a ``QFrame`` purely so we can lean on Qt's styling system
        # to draw the rounded rectangle without writing a custom ``paintEvent``.
        self._label = QLabel(self)
        self._label.setObjectName("floatingToolTipLabel")
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._label.setWordWrap(True)

        self._padding = 6
        self._border_width = 1
        self._corner_radius = 6
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self._padding, self._padding, self._padding, self._padding)
        layout.addWidget(self._label)

        # Respect the palette whenever possible so the tooltip integrates with
        # the current theme while falling back to readable colours when the
        # palette omits dedicated tooltip roles (a common occurrence on Linux).
        palette: QPalette = QGuiApplication.palette()
        background = self._resolve_css_colour(
            palette.color(QPalette.ColorRole.ToolTipBase), "#ffffe1"
        )
        text_colour = self._resolve_css_colour(
            palette.color(QPalette.ColorRole.ToolTipText), "#000000"
        )
        border_colour = self._resolve_css_colour(palette.color(QPalette.ColorRole.Mid), "#999999")

        # Apply the palette-aware styling directly to this frame and its label.
        self.setStyleSheet(
            f"""
            #floatingToolTip {{
                background-color: {background};
                border: {self._border_width}px solid {border_colour};
                border-radius: {self._corner_radius}px;
            }}
            #floatingToolTip QLabel {{
                background-color: transparent;
                color: {text_colour};
            }}
            """
        )

        tooltip_font = QGuiApplication.font("QToolTip")
        self._font = QFont(tooltip_font)
        self._label.setFont(self._font)

        # Constrain wrapping to a sensible width so long strings do not span the
        # entire screen.  ``adjustSize`` will ensure the frame shrinks back down
        # for short snippets.
        self._label.setMaximumWidth(self._MAX_WIDTH - 2 * self._padding)
        self._label.setMinimumWidth(0)

        self._last_text: str = ""
        self.hide()

    @staticmethod
    def _resolve_css_colour(candidate: QColor, fallback: str) -> str:
        """Return an opaque ``#RRGGBB`` colour string using *fallback* when needed."""

        colour = QColor(candidate) if candidate.isValid() else QColor(fallback)
        if colour.alpha() != 255:
            colour.setAlpha(255)
        return colour.name(QColor.NameFormat.HexRgb)

    def setText(self, text: str) -> None:
        """Update the tooltip content and recompute the preferred geometry."""

        normalised = text or ""
        if normalised == self._last_text:
            # Even when the text is unchanged the layout may require a refresh
            # after the widget was hidden, therefore ``adjustSize`` is still
            # invoked to keep the frame tightly wrapped around the label.
            self.adjustSize()
            return

        self._last_text = normalised
        self._label.setText(normalised)
        self.adjustSize()

    def sizeHint(self) -> QSize:  # noqa: D401 - Qt documents the contract
        """Qt override: compute the popup size for the current label text."""

        if not self._last_text:
            edge = 2 * (self._padding + self._border_width)
            return QSize(edge, edge)

        metrics = QFontMetrics(self._font)
        text_rect = metrics.boundingRect(
            QRect(0, 0, self._MAX_WIDTH - 2 * self._padding, 0),
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            self._last_text,
        )
        width = text_rect.width() + 2 * (self._padding + self._border_width)
        height = text_rect.height() + 2 * (self._padding + self._border_width)
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:  # noqa: D401 - mirrors :meth:`sizeHint`
        """Qt override: defer to :meth:`sizeHint` for layout calculations."""

        return self.sizeHint()

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
        """Hide the popup without discarding the cached label text."""

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
