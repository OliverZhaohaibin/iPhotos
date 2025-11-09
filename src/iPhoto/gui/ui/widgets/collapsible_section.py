"""Collapsible tool section widget with rotating arrow indicators."""

from __future__ import annotations

from typing import Optional


from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette, QFont
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
from ..palette import Edit_SIDEBAR_SUB_FONT


class CollapsibleSection(QFrame):
    """Display a titled header that can expand and collapse a content widget."""

    def __init__(
        self,
        title: str,
        icon_name: str,
        content: QWidget,
        parent: Optional[QWidget] = None,
        title_font: Optional[QFont] = None,
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
        # ``_toggle_icon_tint`` retains the optional colour override supplied by
        # the edit controller when the application switches to the dark theme.
        # The override ensures the arrow glyph stays legible after the user
        # expands or collapses the section, because the icon is reloaded on
        # every state change.
        self._toggle_icon_tint: str | None = None

        header_layout.addWidget(self._toggle_button)

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

        header_layout.addStretch(1)
        self._custom_controls_layout = QHBoxLayout()
        self._custom_controls_layout.setContentsMargins(0, 0, 0, 0)
        self._custom_controls_layout.setSpacing(4)
        header_layout.addLayout(self._custom_controls_layout)
        self._title_label.setFont(title_font or Edit_SIDEBAR_SUB_FONT)

        self._header.mouseReleaseEvent = self._forward_click_to_button  # type: ignore[assignment]
        layout.addWidget(self._header)

        self._content_frame = QFrame(self)
        content_layout = QVBoxLayout(self._content_frame)
        # The content frame acts as a pure animation wrapper, therefore any padding must live on
        # the embedded widget itself.  Keeping margins or spacing here would introduce an initial
        # layout jump when the frame transitions from hidden to visible because Qt applies the
        # extra space before the height animation has a chance to interpolate it smoothly.
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._content)
        layout.addWidget(self._content_frame)


        self._expanded = True
        self._update_header_icon()
        self._update_content_geometry()

    # ------------------------------------------------------------------
    def add_header_control(self, widget: QWidget) -> None:
        """Place *widget* on the right side of the header, before the arrow button."""

        layout = self._header.layout()
        if layout is None:
            return
        self._custom_controls_layout.addWidget(widget)

    # ------------------------------------------------------------------
    def set_expanded(self, expanded: bool) -> None:
        """Expand or collapse the section to match *expanded*."""

        if self._expanded == expanded:
            return
        self._expanded = expanded


        self._update_header_icon()

        QTimer.singleShot(0, self._update_content_geometry)

    def is_expanded(self) -> bool:
        """Return ``True`` when the section currently displays its content."""

        return self._expanded

    def toggle(self) -> None:
        """Invert the expansion state to show or hide the content widget."""

        self.set_expanded(not self._expanded)


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
        """Initialise or UPDATE the content frame height to match the widget state."""

        if self._expanded:
            # When the section starts expanded we must immediately unlock the maximum height to
            # Qt's documented ``QWIDGETSIZE_MAX`` value.  The initial size hint only captures the
            # geometry of the currently visible children (for example the collapsed Light options),
            # so freezing ``maximumHeight`` to that measurement would prevent nested collapsible
            # sections from expanding until the parent section is collapsed and reopened.  By
            # setting the limit to ``16777215`` up front we match the behaviour applied after the
            # animation completes and guarantee that child widgets can freely grow during the first
            # interaction.
            self._content_frame.setMaximumHeight(16777215)
            self._content_frame.setVisible(True)
        else:
            self._content_frame.setMaximumHeight(0)
            self._content_frame.hide()

    def _forward_click_to_button(self, event) -> None:  # pragma: no cover - GUI glue
        """Treat header clicks as if the toggle button itself was pressed."""

        del event  # The button click does not need the event object.
        self._toggle_button.click()


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

class CollapsibleSubSection(CollapsibleSection):
    def __init__(
        self,
        title: str,
        icon_name: str,
        content: QWidget,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(
            title=title,
            icon_name=icon_name,
            content=content,
            parent=parent,
            title_font=Edit_SIDEBAR_SUB_FONT
        )