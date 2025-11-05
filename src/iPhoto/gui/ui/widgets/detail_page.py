"""Detail page showing the focused asset with related controls."""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QSlider,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..icon import load_icon
from ..palette import viewer_surface_color
from .filmstrip_view import FilmstripView
from .gl_image_viewer import GLImageViewer
from .live_badge import LiveBadge
from .main_window_metrics import HEADER_BUTTON_SIZE, HEADER_ICON_GLYPH_SIZE
from .video_area import VideoArea


class DetailPageWidget(QWidget):
    """Composite widget that mirrors the behaviour of the original detail page."""

    def __init__(self, main_window: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detailPage")

        # Header widgets -----------------------------------------------------
        self.back_button = QToolButton(self)
        self.info_button = QToolButton(self)
        self.share_button = QToolButton(self)
        self.favorite_button = QToolButton(self)
        self.favorite_button.setEnabled(False)
        self.edit_button = QToolButton(self)
        self.edit_button.setEnabled(False)

        self.zoom_widget = QWidget(self)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self.zoom_widget)
        self.zoom_in_button = QToolButton(self.zoom_widget)
        self.zoom_out_button = QToolButton(self.zoom_widget)

        self.location_label = QLabel(self)
        self.timestamp_label = QLabel(self)

        # Viewer widgets -----------------------------------------------------
        self.player_stack = QStackedWidget(self)
        self.player_placeholder = QLabel("Select a photo or video to preview.", self.player_stack)
        self.image_viewer = GLImageViewer()
        self.video_area = VideoArea()
        self.player_bar = self.video_area.player_bar

        self.filmstrip_view = FilmstripView()

        self.live_badge = LiveBadge(main_window)
        self.live_badge.hide()
        self.badge_host: QWidget | None = None

        # References controllers rely on when shuffling widgets.
        self.detail_actions_layout: QHBoxLayout | None = None
        self.detail_info_button_index = -1
        self.detail_favorite_button_index = -1
        self.detail_header_layout: QHBoxLayout | None = None
        self.detail_zoom_widget_index = -1
        self.detail_header: QWidget | None = None
        self.detail_chrome_container: QWidget | None = None
        self.detail_header_separator: QFrame | None = None
        self.player_container: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._build_header(main_window, layout)
        self._build_player_area(layout)

    def _build_header(self, main_window: QWidget, parent_layout: QVBoxLayout) -> None:
        """Create the header row containing navigation and metadata controls."""

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 12, 0)
        header_layout.setSpacing(8)

        self._configure_header_button(
            self.back_button,
            "chevron.left.svg",
            "Return to grid view",
        )
        header_layout.addWidget(self.back_button)

        info_container = QWidget(header)
        info_layout = QVBoxLayout(info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        info_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        base_font = main_window.font()
        location_font = QFont(base_font)
        if location_font.pointSize() > 0:
            location_font.setPointSize(location_font.pointSize() + 2)
        else:
            location_font.setPointSize(14)
        location_font.setBold(True)

        timestamp_font = QFont(base_font)
        if timestamp_font.pointSize() > 0:
            timestamp_font.setPointSize(max(timestamp_font.pointSize() + 1, 1))
        else:
            timestamp_font.setPointSize(12)
        timestamp_font.setBold(False)

        self.location_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.location_label.setFont(location_font)
        self.location_label.setVisible(False)

        self.timestamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timestamp_label.setFont(timestamp_font)
        self.timestamp_label.setVisible(False)

        info_layout.addWidget(self.location_label)
        info_layout.addWidget(self.timestamp_label)

        zoom_layout = QHBoxLayout(self.zoom_widget)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(4)

        small_button_size = QSize(
            int(HEADER_BUTTON_SIZE.width() / 2),
            int(HEADER_BUTTON_SIZE.height() / 2),
        )
        self._configure_header_button(self.zoom_out_button, "minus.svg", "Zoom Out")
        self.zoom_out_button.setFixedSize(small_button_size)
        zoom_layout.addWidget(self.zoom_out_button)

        self.zoom_slider.setRange(10, 400)
        self.zoom_slider.setSingleStep(5)
        self.zoom_slider.setPageStep(25)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(90)
        self.zoom_slider.setToolTip("Zoom")
        zoom_layout.addWidget(self.zoom_slider)

        self._configure_header_button(self.zoom_in_button, "plus.svg", "Zoom In")
        self.zoom_in_button.setFixedSize(small_button_size)
        zoom_layout.addWidget(self.zoom_in_button)

        actions_container = QWidget(header)
        actions_layout = QHBoxLayout(actions_container)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)

        for button, icon_name, tooltip in (
            (self.info_button, "info.circle.svg", "Info"),
            (self.share_button, "square.and.arrow.up.svg", "Share"),
            (self.favorite_button, "suit.heart.svg", "Add to Favorites"),
            (self.edit_button, "slider.horizontal.3.svg", "Edit"),
        ):
            self._configure_header_button(button, icon_name, tooltip)
            actions_layout.addWidget(button)

        self.detail_actions_layout = actions_layout
        self.detail_info_button_index = actions_layout.indexOf(self.info_button)
        self.detail_favorite_button_index = actions_layout.indexOf(self.favorite_button)

        header_layout.addWidget(self.zoom_widget)
        self.zoom_widget.hide()
        header_layout.addWidget(info_container, 1)
        header_layout.addWidget(actions_container)
        self.detail_header_layout = header_layout
        self.detail_zoom_widget_index = header_layout.indexOf(self.zoom_widget)

        detail_chrome_container = QWidget(self)
        detail_chrome_layout = QVBoxLayout(detail_chrome_container)
        detail_chrome_layout.setContentsMargins(0, 0, 0, 0)
        detail_chrome_layout.setSpacing(6)
        detail_chrome_layout.addWidget(header)
        self.detail_header = header

        header_separator = QFrame(detail_chrome_container)
        header_separator.setObjectName("detailHeaderSeparator")
        header_separator.setFrameShape(QFrame.Shape.HLine)
        header_separator.setFrameShadow(QFrame.Shadow.Plain)
        header_separator.setFixedHeight(2)
        base_surface = viewer_surface_color(self)
        separator_tint = QColor(base_surface).darker(108)
        header_separator.setStyleSheet(
            "QFrame#detailHeaderSeparator {"
            f"  background-color: {separator_tint.name()};"
            "  border: none;"
            "}"
        )
        separator_shadow = QGraphicsDropShadowEffect(header_separator)
        separator_shadow.setBlurRadius(14)
        separator_shadow.setColor(QColor(0, 0, 0, 45))
        separator_shadow.setOffset(0, 1)
        header_separator.setGraphicsEffect(separator_shadow)
        detail_chrome_layout.addWidget(header_separator)
        self.detail_header_separator = header_separator

        parent_layout.addWidget(detail_chrome_container)
        self.detail_chrome_container = detail_chrome_container

    def _build_player_area(self, parent_layout: QVBoxLayout) -> None:
        """Create the stacked media viewer and accompanying filmstrip."""

        self.player_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.player_placeholder.setStyleSheet(
            "background-color: palette(window); "
            "color: palette(window-text); font-size: 16px;"
        )
        self.player_placeholder.setMinimumHeight(320)

        self.player_stack.addWidget(self.player_placeholder)
        self.player_stack.addWidget(self.image_viewer)
        self.player_stack.addWidget(self.video_area)
        self.player_stack.setCurrentWidget(self.player_placeholder)

        player_container = QWidget(self)
        player_layout = QVBoxLayout(player_container)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.setSpacing(0)
        player_layout.addWidget(self.player_stack)
        parent_layout.addWidget(player_container)
        self.player_container = player_container

        parent_layout.addWidget(self.filmstrip_view)

        self.live_badge.setParent(player_container)
        self.badge_host = player_container
        self.live_badge.raise_()

    def _configure_header_button(self, button: QToolButton, icon_name: str, tooltip: str) -> None:
        """Normalize header button appearance to the design defaults."""

        button.setIcon(load_icon(icon_name))
        button.setIconSize(HEADER_ICON_GLYPH_SIZE)
        button.setFixedSize(HEADER_BUTTON_SIZE)
        button.setAutoRaise(True)
        button.setToolTip(tooltip)


__all__ = ["DetailPageWidget"]
