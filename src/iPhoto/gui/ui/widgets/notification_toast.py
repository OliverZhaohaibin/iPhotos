"""Transient animated toast displayed to confirm clipboard actions."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    Property,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from ..icons import load_icon


class NotificationToast(QWidget):
    """Fade-in/out toast that floats above the main window.

    The widget renders an opaque rounded rectangle with a checkmark glyph and an
    optional message.  Calling :meth:`show_toast` performs a full animation cycle:

    1. Instantly position the toast in the centre of its parent window.
    2. Fade in over 200 ms using a subtle ease curve to avoid abrupt motion.
    3. Stay fully visible for the configured dwell time (default 1.2 seconds).
    4. Fade out over 300 ms and hide the widget when the animation completes.

    The toast never steals focus and is rendered as a tool window so it floats on
    top of the application without appearing in the task switcher.
    """

    _DEFAULT_WIDTH = 220
    _DEFAULT_HEIGHT = 220
    _DEFAULT_DWELL_MS = 1200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )

        # Allow the toast to draw with a translucent background and prevent it from
        # ever acquiring focus.  Both flags ensure the notification feels lightweight
        # and non-modal despite being rendered in a separate window.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._background_color = QColor(0, 0, 0, 190)
        self._text_color = QColor(Qt.GlobalColor.white)
        self._corner_radius = 16.0
        self._text = ""

        self.setFixedSize(self._DEFAULT_WIDTH, self._DEFAULT_HEIGHT)

        # Load the checkmark icon once so we can simply repaint it with a clipping
        # animation.  The SVG asset is supplied externally and may be tinted by the
        # shared icon loader so the toast appearance stays consistent with the rest
        # of the UI chrome.
        self._checkmark_icon = load_icon("checkmark.svg")
        self._checkmark_progress = 0.0
        self._checkmark_animation = QPropertyAnimation(self, b"checkmark_progress")
        self._checkmark_animation.setDuration(300)
        self._checkmark_animation.setStartValue(0.0)
        self._checkmark_animation.setEndValue(1.0)
        self._checkmark_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

        # Prepare fade animations once so subsequent toasts simply restart them.
        self._fade_in_animation = QPropertyAnimation(self, b"windowOpacity")
        self._fade_in_animation.setDuration(200)
        self._fade_in_animation.setStartValue(0.0)
        self._fade_in_animation.setEndValue(1.0)
        self._fade_in_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

        self._fade_out_animation = QPropertyAnimation(self, b"windowOpacity")
        self._fade_out_animation.setDuration(300)
        self._fade_out_animation.setStartValue(1.0)
        self._fade_out_animation.setEndValue(0.0)
        self._fade_out_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_out_animation.finished.connect(self.hide)

        # Use a single-shot timer to schedule the fade-out once the toast has been
        # visible long enough.  Restarting the timer allows repeated toasts to extend
        # their lifetime if the user triggers clipboard actions in quick succession.
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self._DEFAULT_DWELL_MS)
        self._hide_timer.timeout.connect(self._fade_out_animation.start)

    def _get_checkmark_progress(self) -> float:
        """Return the current left-to-right reveal ratio for the icon stroke."""

        return self._checkmark_progress

    def _set_checkmark_progress(self, value: float) -> None:
        """Update the icon reveal progress and schedule a repaint."""

        self._checkmark_progress = max(0.0, min(1.0, float(value)))
        self.update()

    checkmark_progress = Property(float, _get_checkmark_progress, _set_checkmark_progress)

    def event(self, event: QEvent) -> bool:  # type: ignore[override]
        """Ignore close requests while we are animating.

        Qt may issue spontaneous close events when the parent window hides.  We
        intercept these events to ensure :meth:`hide` is only called after the
        fade-out animation has completed.  Returning ``True`` marks the event as
        handled when we are actively animating.
        """

        if event.type() == QEvent.Type.Close and self._fade_out_animation.state():
            return True
        return super().event(event)

    def show_toast(self, text: str) -> None:
        """Display the toast with *text* centred over the parent window."""

        self._text = text
        self.update()

        # If animations are mid-flight we stop them to avoid abrupt opacity jumps.
        for animation in (
            self._fade_in_animation,
            self._fade_out_animation,
            self._checkmark_animation,
        ):
            if animation.state():
                animation.stop()
        self._hide_timer.stop()

        # Reset the checkmark so each toast replays the drawing animation from the
        # beginning.  ``setProperty`` ensures the Qt animation framework sees the
        # updated baseline before the next ``start`` call.
        self.setProperty("checkmark_progress", 0.0)

        parent = self.parentWidget()
        if parent is not None:
            center = parent.geometry().center()
            self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)

        self.setWindowOpacity(0.0)
        self.show()
        self._fade_in_animation.start()
        self._checkmark_animation.start()
        self._hide_timer.start()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        """Render the rounded rectangle, checkmark glyph, and caption text."""

        del event  # Unused Qt paint event placeholder.

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Draw a translucent rounded rectangle background so the toast floats over
        # the existing UI while remaining legible regardless of the underlying scene.
        rect = self.rect()
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), self._corner_radius, self._corner_radius)
        painter.fillPath(path, self._background_color)

        # Paint the checkmark SVG while progressively revealing it from left to
        # right.  Clipping the painter is significantly cheaper than attempting to
        # sample the SVG path geometry and keeps the animation compatible with any
        # future icon redesigns.
        icon_rect = QRect(
            0,
            int(self.height() * 0.15),
            self.width(),
            int(self.height() * 0.4),
        )
        clip_width = int(icon_rect.width() * self._checkmark_progress)
        if clip_width > 0 and not self._checkmark_icon.isNull():
            painter.save()
            painter.setClipRect(
                QRect(icon_rect.left(), icon_rect.top(), clip_width, icon_rect.height())
            )
            self._checkmark_icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)
            painter.restore()

        # Render the caption beneath the icon using a bold sans-serif font.
        font = QFont(self.font())
        font.setPixelSize(22)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self._text_color)

        text_rect = QRect(
            rect.x(),
            int(rect.height() * 0.65),
            rect.width(),
            int(rect.height() * 0.25),
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self._text)
