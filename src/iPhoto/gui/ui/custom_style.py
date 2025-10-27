"""Custom Qt style helpers that refine core widgets such as scroll bars."""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QProxyStyle,
    QStyle,
    QStyleFactory,
    QStyleOptionSlider,
    QWidget,
)


class CustomScrollBarStyle(QProxyStyle):
    """Proxy style that renders rounded, theme-aware scroll bars.

    Frameless windows that rely on ``WA_TranslucentBackground`` often cause the
    platform styles to ignore stylesheet hints for ``QScrollBar`` widgets.  This
    proxy intercepts the painting routines so we can force the Windows 11 style
    capsule handle and rounded groove while delegating every other primitive to
    the underlying base style.
    """

    _HANDLE_MIN_LENGTH_PX = 25
    _TRACK_THICKNESS_PX = 10

    def __init__(self, base_style: QStyle | None = None) -> None:
        """Initialise the proxy with an optional ``base_style`` delegate."""

        resolved_base = base_style
        resolved_base_name: str | None = None

        if resolved_base is not None:
            resolved_base_name = resolved_base.objectName()
            if resolved_base_name.lower() == "windowsvista":
                fusion_style = QStyleFactory.create("Fusion")
                if fusion_style is not None:
                    resolved_base = fusion_style
                    resolved_base_name = fusion_style.objectName()

        if resolved_base is None:
            resolved_base = QStyleFactory.create("Fusion")
            if resolved_base is not None:
                resolved_base_name = resolved_base.objectName()

        if resolved_base is None:
            raise RuntimeError("Unable to resolve a base style for CustomScrollBarStyle")

        super().__init__()
        # ``QProxyStyle`` does not hold a reference to the delegate, therefore we keep
        # a handle on ``self`` so the Python wrapper stays alive for the lifetime of
        # the proxy.  Without this guard Qt may attempt to access a deleted object
        # during later paint events.
        self._base_delegate = resolved_base
        self.setBaseStyle(resolved_base)

        if __debug__:
            debug_name = resolved_base_name or self.baseStyle().objectName()
            print(f"CustomScrollBarStyle using base style: {debug_name}")

    # ------------------------------------------------------------------
    # Colour helpers
    # ------------------------------------------------------------------
    def _resolve_handle_colours(
        self, option: QStyleOptionSlider, widget: QWidget | None
    ) -> tuple[QColor, QColor, QColor]:
        """Return fully opaque colours for the handle's interaction states."""

        palette = widget.palette() if widget is not None else option.palette
        window_colour = palette.color(QPalette.ColorRole.Window)
        window_lightness = window_colour.lightness()

        if window_lightness < 128:
            # Dark theme – brighter greys stay visible against deep backgrounds.
            base, hover, pressed = (200, 215, 185)
        else:
            # Light theme – match the capsule greys seen in Windows 11.
            base, hover, pressed = (154, 127, 106)

        def _grey(value: int) -> QColor:
            colour = QColor(value, value, value)
            colour.setAlpha(255)
            return colour

        return (_grey(base), _grey(hover), _grey(pressed))

    def _resolve_groove_colour(
        self, option: QStyleOptionSlider, widget: QWidget | None
    ) -> QColor:
        """Return a translucent groove colour derived from the palette."""

        palette = widget.palette() if widget is not None else option.palette
        window_colour = palette.color(QPalette.ColorRole.Window)
        groove_colour = QColor(window_colour)
        if window_colour.lightness() < 128:
            groove_colour = groove_colour.lighter(160)
        else:
            groove_colour = groove_colour.darker(110)
        groove_colour.setAlpha(150)
        return groove_colour

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    def _capsule_rect(self, option: QStyleOptionSlider) -> QRect:
        """Return the shared capsule geometry used for the handle and groove."""

        # Leaving a one-pixel inset on every edge keeps the painted capsule away
        # from the viewport border so the anti-aliased corners remain visible on
        # all scaling factors.
        return option.rect.adjusted(1, 1, -1, -1)

    def _capsule_radius(self, rect: QRect) -> float:
        """Return the radius that turns *rect* into a perfect capsule."""

        # The capsule should mirror Windows 11 where the corner radius equals
        # half of the available thickness.  ``max`` guards against zero-sized
        # rectangles that may appear during layout transitions.
        return max(1.0, min(rect.width(), rect.height()) / 2.0)

    # ------------------------------------------------------------------
    # Painting helpers
    # ------------------------------------------------------------------
    def _paint_handle(
        self,
        option: QStyleOptionSlider,
        painter: QPainter,
        widget: QWidget | None,
    ) -> None:
        """Paint a rounded capsule that mimics the Windows 11 scroll bar handle."""

        normal_colour, hover_colour, pressed_colour = self._resolve_handle_colours(
            option, widget
        )

        if option.state & QStyle.StateFlag.State_Sunken:
            brush_colour = pressed_colour
        elif option.state & QStyle.StateFlag.State_MouseOver:
            brush_colour = hover_colour
        else:
            brush_colour = normal_colour

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(brush_colour)
        painter.setClipRect(option.rect)
        capsule_rect = self._capsule_rect(option)
        radius = self._capsule_radius(capsule_rect)
        painter.drawRoundedRect(capsule_rect, radius, radius)
        painter.restore()

    def _paint_groove(
        self,
        option: QStyleOptionSlider,
        painter: QPainter,
        widget: QWidget | None,
    ) -> None:
        """Paint the rounded track that sits underneath the handle."""

        groove_colour = self._resolve_groove_colour(option, widget)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(groove_colour)
        painter.setClipRect(option.rect)
        capsule_rect = self._capsule_rect(option)
        radius = self._capsule_radius(capsule_rect)
        painter.drawRoundedRect(capsule_rect, radius, radius)
        painter.restore()

    # ------------------------------------------------------------------
    # QStyle overrides
    # ------------------------------------------------------------------
    def drawControl(
        self,
        element: QStyle.ControlElement,
        option: QStyleOptionSlider,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:  # type: ignore[override]
        """Render scroll bar components with rounded geometry."""

        if element == QStyle.ControlElement.CE_ScrollBarSlider and isinstance(
            option, QStyleOptionSlider
        ):
            # Some base styles paint the slider via ``drawControl`` so we handle the
            # request here and exit early to prevent the delegate from drawing again.
            self._paint_handle(option, painter, widget)
            return

        if element in (
            QStyle.ControlElement.CE_ScrollBarAddPage,
            QStyle.ControlElement.CE_ScrollBarSubPage,
        ) and isinstance(option, QStyleOptionSlider):
            self._paint_groove(option, painter, widget)
            return

        if element in (
            QStyle.ControlElement.CE_ScrollBarAddLine,
            QStyle.ControlElement.CE_ScrollBarSubLine,
        ):
            # Skip the legacy arrow buttons entirely to mirror modern scroll bars.
            return

        super().drawControl(element, option, painter, widget)

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOptionSlider,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:  # type: ignore[override]
        """Override primitive painting so native delegates cannot bypass us."""

        if element == QStyle.PrimitiveElement.PE_IndicatorScrollBarSlider and isinstance(
            option, QStyleOptionSlider
        ):
            # Many platform styles render the handle via ``drawPrimitive`` rather than
            # ``drawControl``.  Reusing the helper guarantees we cover both paths.
            self._paint_handle(option, painter, widget)
            return

        if element in (
            QStyle.PrimitiveElement.PE_IndicatorArrowUp,
            QStyle.PrimitiveElement.PE_IndicatorArrowDown,
            QStyle.PrimitiveElement.PE_IndicatorArrowLeft,
            QStyle.PrimitiveElement.PE_IndicatorArrowRight,
        ):
            # Prevent the base style from painting arrow glyphs to keep the minimal look.
            return

        super().drawPrimitive(element, option, painter, widget)

    def pixelMetric(self, metric: QStyle.PixelMetric, option=None, widget=None) -> int:
        """Provide geometry overrides so the proxy can honour our margins."""

        if metric == QStyle.PixelMetric.PM_ScrollBarExtent:
            # ``PM_ScrollBarExtent`` controls the overall thickness of the bar.  We draw
            # directly inside the provided geometry, so we can return the capsule
            # thickness without applying extra padding.
            return self._TRACK_THICKNESS_PX

        if metric in (
            QStyle.PixelMetric.PM_ScrollBarAddLineExtent,
            QStyle.PixelMetric.PM_ScrollBarSubLineExtent,
        ):
            # Returning zero removes the fixed-size arrow regions entirely, matching the
            # clean Windows 11 presentation.
            return 0

        if metric == QStyle.PixelMetric.PM_ScrollView_ScrollBarSpacing:
            # Collapse the gap between the scroll bar and the viewport so the groove
            # appears embedded in the content area.
            return 0

        if metric == QStyle.PixelMetric.PM_ScrollBarSliderMin:
            return self._HANDLE_MIN_LENGTH_PX

        return super().pixelMetric(metric, option, widget)

    def styleHint(self, hint: QStyle.StyleHint, option=None, widget=None, returnData=None):
        """Adjust behavioural hints so the proxy matches modern scroll bars."""

        if hint == QStyle.StyleHint.SH_ScrollBar_Transient:
            # ``0`` disables overlay scroll bars so Qt reserves layout space for the
            # groove, matching the desktop behaviour showcased in the design brief.
            return 0

        if hint == QStyle.StyleHint.SH_ScrollBar_ContextMenu:
            # The classic context menu exposes options for the hidden arrow buttons.
            # Returning ``0`` prevents it from showing and keeps the UX consistent.
            return 0

        return super().styleHint(hint, option, widget, returnData)


__all__ = ["CustomScrollBarStyle"]
