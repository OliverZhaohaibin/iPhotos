"""Collapsible tool section widget with rotating arrow indicators."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..icon import load_icon


class CollapsibleSection(QFrame):
    """Display a titled header that can expand and collapse a content widget."""

    def __init__(
        self,
        title: str,
        icon_name: str,
        content: QWidget,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("collapsibleSection")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._content = content
        self._content.setParent(self)
        self._content.setVisible(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QWidget(self)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(4, 4, 4, 4)
        header_layout.setSpacing(8)

        self._toggle_button = QToolButton(self._header)
        self._toggle_button.setAutoRaise(True)
        self._toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_button.setIcon(load_icon("chevron.down.svg"))
        self._toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._toggle_button.clicked.connect(self.toggle)
        header_layout.addWidget(self._toggle_button)
        # ``_toggle_icon_tint`` retains the optional colour override supplied by
        # the edit controller when the application switches to the dark theme.
        # The override ensures the arrow glyph stays legible after the user
        # expands or collapses the section, because the icon is reloaded on
        # every state change.
        self._toggle_icon_tint: str | None = None

        icon = load_icon(icon_name)
        icon_label = QLabel(self._header)
        icon_label.setPixmap(icon.pixmap(20, 20))
        icon_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        header_layout.addWidget(icon_label)
        # ``_icon_label`` and ``_icon_name`` are retained so other components can recolour the
        # header icon when the global theme changes (for example the edit controller's dark mode).
        self._icon_label = icon_label
        self._icon_name = icon_name

        self._title_label = QLabel(title, self._header)
        title_palette = self._title_label.palette()
        title_palette.setColor(
            QPalette.ColorRole.WindowText,
            title_palette.color(QPalette.ColorRole.Text),
        )
        self._title_label.setPalette(title_palette)
        header_layout.addWidget(self._title_label, 1)

        self._header.mouseReleaseEvent = self._forward_click_to_button  # type: ignore[assignment]
        layout.addWidget(self._header)

        self._content_frame = QFrame(self)
        content_layout = QVBoxLayout(self._content_frame)
        content_layout.setContentsMargins(8, 0, 8, 12)
        content_layout.setSpacing(8)
        content_layout.addWidget(self._content)
        layout.addWidget(self._content_frame)

        self._animation = QPropertyAnimation(self._content_frame, b"maximumHeight", self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._animation.finished.connect(self._on_animation_finished)

        self._expanded = True
        self._update_header_icon()
        self._update_content_geometry()

    # ------------------------------------------------------------------
    def set_expanded(self, expanded: bool) -> None:
        """Expand or collapse the section to match *expanded*."""

        if self._expanded == expanded:
            return
        self._expanded = expanded
        self._update_header_icon()
        self._animate_content(expanded)

    def is_expanded(self) -> bool:
        """Return ``True`` when the section currently displays its content."""

        return self._expanded

    def toggle(self) -> None:
        """Invert the expansion state to show or hide the content widget."""

        self.set_expanded(not self._expanded)

    # ------------------------------------------------------------------
    def _animate_content(self, expanded: bool) -> None:
        """Animate the content frame between collapsed and expanded states."""

        self._animation.stop()
        start_height = self._content_frame.maximumHeight()
        if start_height <= 0:
            start_height = self._content.sizeHint().height()
        end_height = self._content.sizeHint().height() if expanded else 0
        if expanded:
            self._content_frame.setVisible(True)
        self._animation.setStartValue(start_height)
        self._animation.setEndValue(end_height)
        self._animation.start()

    def _update_header_icon(self) -> None:
        """Refresh the arrow glyph so it reflects the expansion state."""

        icon_name = "chevron.down.svg" if self._expanded else "chevron.right.svg"
        if self._toggle_icon_tint is None:
            self._toggle_button.setIcon(load_icon(icon_name))
        else:
            self._toggle_button.setIcon(
                load_icon(icon_name, color=self._toggle_icon_tint)
            )

    def _update_content_geometry(self) -> None:
        """Initialise the content frame height to match the widget state."""

        if self._expanded:
            self._content_frame.setMaximumHeight(self._content.sizeHint().height())
            self._content_frame.setVisible(True)
        else:
            self._content_frame.setMaximumHeight(0)
            self._content_frame.hide()

    def _forward_click_to_button(self, event) -> None:  # pragma: no cover - GUI glue
        """Treat header clicks as if the toggle button itself was pressed."""

        del event  # The button click does not need the event object.
        self._toggle_button.click()

    def _on_animation_finished(self) -> None:  # pragma: no cover - GUI glue
        """Hide the content frame after collapsing to keep layouts tight."""

        if not self._expanded:
            self._content_frame.hide()

    # ------------------------------------------------------------------
    def set_toggle_icon_tint(self, tint: QColor | str | None) -> None:
        """Set *tint* as the colour override for the arrow icon.

        The edit controller forces collapsible section headers to use bright
        icons while the dark theme is active.  This helper caches the
        normalised colour (stored as a hexadecimal ARGB string) so future
        expansion state changes reuse the same tint.  Passing ``None`` clears
        the override and returns the icon to its default styling.
        """

        if tint is None:
            self._toggle_icon_tint = None
        else:
            if isinstance(tint, QColor):
                tint_hex = tint.name(QColor.NameFormat.HexArgb)
            else:
                tint_hex = str(tint)
            self._toggle_icon_tint = tint_hex
        self._update_header_icon()
