"""Segmented control with a sliding highlight that tracks the checked action."""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, QRect, Qt
from PySide6.QtGui import QAction, QColor, QPalette
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QToolButton, QWidget

class SlidingSegmentedControl(QWidget):
    """Arrange checkable actions with a movable background highlight."""

    def __init__(self, actions: Iterable[QAction], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._buttons: list[QToolButton] = []
        self._highlight = QWidget(self)
        self._highlight.setObjectName("segmentedHighlight")
        self._highlight.hide()
        self._highlight.lower()

        palette = self.palette()
        base = palette.color(QPalette.ColorRole.Base)
        palette.setColor(QPalette.ColorRole.Window, base)
        self.setPalette(palette)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        for action in actions:
            button = QToolButton(self)
            button.setDefaultAction(action)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.toggled.connect(lambda checked, btn=button: self._on_button_toggled(btn, checked))
            button.installEventFilter(self)
            layout.addWidget(button)
            self._buttons.append(button)

        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        self._animation = QPropertyAnimation(self._highlight, b"geometry", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._sync_initial_highlight()

    # ------------------------------------------------------------------
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # pragma: no cover - Qt glue
        """Ensure the highlight keeps tracking buttons as they resize."""

        if event.type() == QEvent.Type.Resize and watched in self._buttons:
            if watched.isChecked():
                self._move_highlight_to_button(watched, animate=False)
        return super().eventFilter(watched, event)

    def sync_to_checked_action(self) -> None:
        """Realign the highlight to whichever action is currently checked."""

        for button in self._buttons:
            if button.isChecked():
                self._move_highlight_to_button(button, animate=False)
                return
        if self._buttons:
            self._move_highlight_to_button(self._buttons[0], animate=False)

    # ------------------------------------------------------------------
    def _sync_initial_highlight(self) -> None:
        """Show the highlight in the correct location when the widget loads."""

        self.sync_to_checked_action()
        self._update_palette()

    def _on_button_toggled(self, button: QToolButton, checked: bool) -> None:
        """Animate the background highlight whenever a button becomes active."""

        if not checked:
            return
        self._move_highlight_to_button(button, animate=True)

    def _move_highlight_to_button(self, button: QToolButton, *, animate: bool) -> None:
        """Place the highlight under *button*, optionally with animation."""

        if not self._buttons:
            return

        target_rect = button.geometry()
        if target_rect.isNull():
            return

        padded = QRect(target_rect)
        padded.adjust(-2, -2, 2, 2)
        padded.setHeight(max(padded.height(), button.sizeHint().height() + 4))

        if not self._highlight.isVisible():
            self._highlight.setGeometry(padded)
            self._highlight.show()
            return

        self._animation.stop()
        if animate:
            self._animation.setStartValue(self._highlight.geometry())
            self._animation.setEndValue(padded)
            self._animation.start()
        else:
            self._highlight.setGeometry(padded)

    def _update_palette(self) -> None:
        """Tint the highlight using the current application accent colour."""

        palette = self.palette()
        highlight_color = palette.color(QPalette.ColorRole.Highlight)
        blend = QColor(highlight_color)
        blend.setAlpha(90)
        highlight_stylesheet = "\n".join(
            [
                "#segmentedHighlight {",
                f"background-color: {blend.name(QColor.NameFormat.HexArgb)};",
                "border-radius: 6px;",
                "}",
            ]
        )
        control_stylesheet = "\n".join(
            [
                "SlidingSegmentedControl {",
                "background-color: palette(window);",
                "border-radius: 8px;",
                "}",
            ]
        )

        self._highlight.setStyleSheet(highlight_stylesheet)
        self.setStyleSheet(control_stylesheet)
