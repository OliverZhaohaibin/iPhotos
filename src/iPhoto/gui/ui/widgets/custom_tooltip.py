"""Floating tooltip widget that sidesteps ``QToolTip`` transparency issues."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QLabel


class FloatingToolTip(QLabel):
    """Small top-level label that mimics ``QToolTip`` using explicit painting.

    ``QToolTip`` inherits the ``WA_TranslucentBackground`` attribute from the
    frameless main window which prevents Qt from filling its background on
    certain platforms, leaving the popup as a solid black rectangle.  This
    helper widget replaces the system tooltip with a regular ``QLabel`` and
    manually opts into the window flags a tooltip requires.  Because the label
    is responsible for painting its background it is unaffected by the main
    window's transparency settings.
    """

    _CURSOR_OFFSET = QPoint(14, 22)

    def __init__(self) -> None:
        # ``Qt.ToolTip`` marks the widget as transient and keeps it above the
        # owning window without taking focus.  ``FramelessWindowHint`` prevents
        # native decorations from showing up which would immediately break the
        # tooltip illusion.  ``WindowStaysOnTopHint`` ensures the label is never
        # obscured by the application window when the user moves the mouse
        # rapidly between markers.
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )

        # The tooltip must render its own background to avoid inheriting the
        # translucent backdrop from the frameless window shell.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAutoFillBackground(True)

        # Plain text guarantees the popup never interprets map annotation names
        # as HTML and avoids the overhead of rich text parsing on every hover.
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # ``setWordWrap`` keeps lengthy city names readable by letting the label
        # expand vertically instead of forcing a very wide popup that might push
        # past the screen edge.
        self.setWordWrap(True)

        # ``setMargin`` does not influence style sheet padding which keeps the
        # visual spacing under our control.
        self.setMargin(0)

        self._apply_default_style()
        self.hide()

    def _apply_default_style(self) -> None:
        """Build a palette-aware style sheet with fully opaque fallbacks."""

        palette: QPalette = QGuiApplication.palette()
        background = palette.color(QPalette.ColorRole.ToolTipBase)
        text = palette.color(QPalette.ColorRole.ToolTipText)
        border = palette.color(QPalette.ColorRole.Mid)

        # ``QColor.name`` drops the alpha channel, so we ensure all colours are
        # fully opaque up front to avoid platform differences where translucent
        # palette roles leak through.
        def _opaque(colour: QColor, default: str) -> str:
            if colour.alpha() < 255:
                colour = QColor(colour)
                colour.setAlpha(255)
            name = colour.name()
            return name if name else default

        bg_name = _opaque(background, "#eef3f6")
        text_name = _opaque(text, "#000000")
        border_name = _opaque(border, "#9a9a9a")

        # Padding and radius values mirror the popup menus so the tooltip feels
        # cohesive with the rest of the translucent interface.
        self.setStyleSheet(
            "QLabel {"
            f"background-color: {bg_name};"
            f"color: {text_name};"
            f"border: 1px solid {border_name};"
            "border-radius: 8px;"
            "padding: 6px 10px;"
            "max-width: 360px;"
            "}"
        )

    def show_text(self, global_pos: QPoint, text: str) -> None:
        """Display *text* near *global_pos* while keeping the popup on-screen."""

        if not text:
            self.hide_tooltip()
            return

        self.setText(text)
        self.adjustSize()

        target = QPoint(global_pos)
        target += self._CURSOR_OFFSET

        width = self.width()
        height = self.height()

        screen = QGuiApplication.screenAt(global_pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()

        if screen is not None:
            available = screen.availableGeometry()

            if target.x() + width > available.right():
                target.setX(available.right() - width)

            if target.y() + height > available.bottom():
                # Flip the tooltip above the cursor when it would overflow the
                # bottom edge.  Offsetting by the same amount keeps the tooltip
                # comfortably separated from the pointer while preserving the
                # user's sight line to the hovered marker.
                target.setY(global_pos.y() - height - self._CURSOR_OFFSET.y())

            if target.x() < available.left():
                target.setX(available.left())

            if target.y() < available.top():
                target.setY(available.top())

        self.move(target)
        self.show()
        self.raise_()

    def hide_tooltip(self) -> None:
        """Hide the popup and clear the text to avoid stale state."""

        if not self.isHidden():
            self.hide()
        self.clear()


__all__ = ["FloatingToolTip"]
