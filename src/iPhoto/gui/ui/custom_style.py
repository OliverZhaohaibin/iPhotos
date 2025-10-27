"""Custom Qt style helpers that refine core widgets such as scroll bars."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleFactory, QStyleOptionSlider


class CustomScrollBarStyle(QProxyStyle):
    """Proxy style that renders rounded, theme-aware scroll bars.

    The standard Qt scroll bar style struggles to respect stylesheet rules when
    frameless windows or translucent backgrounds are involved.  This proxy
    intercepts the rendering routines to ensure we always deliver the Windows 11
    inspired capsule handle and soft translucent groove regardless of platform.
    The implementation keeps the underlying base style untouched so every other
    widget retains its familiar appearance.
    """

    _CORNER_RADIUS_PX = 4
    _HANDLE_MIN_LENGTH_PX = 25
    _TRACK_THICKNESS_PX = 10

    def __init__(self, base_style: QStyle | None = None) -> None:
        """Initialise the proxy with an optional *base_style* to delegate to."""

        resolved_base = base_style
        resolved_base_name = None
        if resolved_base is not None:
            # ``objectName`` can only be queried while the underlying C++ instance
            # is alive, so we cache the string immediately and reuse it later for
            # debug logging.
            resolved_base_name = resolved_base.objectName()
            if resolved_base_name.lower() == "windowsvista":
                fusion_style = QStyleFactory.create("Fusion")
                if fusion_style is not None:
                    resolved_base = fusion_style
                    resolved_base_name = fusion_style.objectName()
        if resolved_base is None:
            # ``QProxyStyle`` requires a backing style to forward unhandled
            # primitives.  Falling back to ``Fusion`` guarantees a predictable
            # baseline even when the platform style cannot be instantiated.
            resolved_base = QStyleFactory.create("Fusion")
            if resolved_base is not None:
                resolved_base_name = resolved_base.objectName()
        if resolved_base is None:
            # A final safeguard in case ``Fusion`` could not be created for some
            # reason.  ``QProxyStyle`` accepts ``None`` but that would reintroduce
            # platform specific behaviour that might block rounded handles again.
            raise RuntimeError("Unable to resolve a base style for CustomScrollBarStyle")

        super().__init__()
        # ``QProxyStyle`` does not retain a reference to the base delegate, so we
        # store it on ``self`` to keep the Python wrapper alive for the lifetime of
        # the proxy.  Failing to do so can trigger "Internal C++ object already
        # deleted" errors once the garbage collector runs.
        self._base_delegate = resolved_base
        self.setBaseStyle(resolved_base)
        if __debug__:
            debug_name = resolved_base_name or self.baseStyle().objectName()
            print(f"CustomScrollBarStyle using base style: {debug_name}")

    # ------------------------------------------------------------------
    # Colour helpers
    # ------------------------------------------------------------------
    def _resolve_handle_colours(
        self, option: QStyleOptionSlider, widget
    ) -> tuple[QColor, QColor, QColor]:
        """Return handle colours for normal, hover, and pressed states.

        The widget palette takes precedence when available because certain
        controls update their palette dynamically (for example during theme
        transitions).  Falling back to the option palette maintains compatibility
        with standard ``QStyle`` painting paths.
        """

        palette = widget.palette() if widget is not None else option.palette
        window_colour = palette.color(QPalette.ColorRole.Window)
        if window_colour.lightness() < 128:
            # Dark theme – use lighter greys for contrast.
            base_value, hover_value, pressed_value = 190, 215, 165
        else:
            # Light theme – darker greys ensure the handle remains visible.
            base_value, hover_value, pressed_value = 154, 127, 106

        def _opaque_grey(value: int) -> QColor:
            colour = QColor(value, value, value)
            colour.setAlpha(255)
            return colour

        return (
            _opaque_grey(base_value),
            _opaque_grey(hover_value),
            _opaque_grey(pressed_value),
        )

    def _resolve_groove_colour(
        self, option: QStyleOptionSlider, widget
    ) -> QColor:
        """Return a semi-transparent groove colour derived from the palette."""

        palette = widget.palette() if widget is not None else option.palette
        window_colour = palette.color(QPalette.ColorRole.Window)
        groove_colour = QColor(window_colour)
        if window_colour.lightness() < 128:
            groove_colour = groove_colour.lighter(140)
        else:
            groove_colour = groove_colour.darker(115)
        groove_colour.setAlpha(150)
        return groove_colour

    # ------------------------------------------------------------------
    # QStyle overrides
    # ------------------------------------------------------------------
    def drawControl(
        self,
        element: QStyle.ControlElement,
        option: QStyleOptionSlider,
        painter: QPainter,
        widget=None,
    ) -> None:  # type: ignore[override]
        """Render scroll bar components with rounded geometry."""

        if element == QStyle.ControlElement.CE_ScrollBarSlider and isinstance(
            option, QStyleOptionSlider
        ):
            normal_colour, hover_colour, pressed_colour = self._resolve_handle_colours(
                option, widget
            )

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)

            if option.state & QStyle.StateFlag.State_Sunken:
                painter.setBrush(pressed_colour)
            elif option.state & QStyle.StateFlag.State_MouseOver:
                painter.setBrush(hover_colour)
            else:
                painter.setBrush(normal_colour)

            draw_rect = option.rect
            if __debug__:
                print(
                    "Drawing CE_ScrollBarSlider",
                    f"rect={draw_rect}",
                    f"radius={self._CORNER_RADIUS_PX}",
                    f"colour={painter.brush().color().name()}",
                )

            painter.setClipRect(option.rect)
            painter.drawRoundedRect(
                draw_rect, self._CORNER_RADIUS_PX, self._CORNER_RADIUS_PX
            )
            painter.restore()
            return

        if element in (
            QStyle.ControlElement.CE_ScrollBarAddPage,
            QStyle.ControlElement.CE_ScrollBarSubPage,
        ) and isinstance(option, QStyleOptionSlider):
            groove_colour = self._resolve_groove_colour(option, widget)

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(groove_colour)

            draw_rect = option.rect
            if __debug__:
                print(
                    "Drawing scroll bar groove",
                    f"rect={draw_rect}",
                    f"radius={self._CORNER_RADIUS_PX}",
                    f"colour={groove_colour.name(QColor.HexArgb)}",
                )

            painter.setClipRect(option.rect)
            painter.drawRoundedRect(
                draw_rect, self._CORNER_RADIUS_PX, self._CORNER_RADIUS_PX
            )
            painter.restore()
            return

        if element in (
            QStyle.ControlElement.CE_ScrollBarAddLine,
            QStyle.ControlElement.CE_ScrollBarSubLine,
        ):
            # Returning early without painting suppresses the legacy arrow buttons.
            return

        super().drawControl(element, option, painter, widget)

    def pixelMetric(self, metric: QStyle.PixelMetric, option=None, widget=None) -> int:
        """Provide geometry overrides so the proxy can honour our margins."""

        if metric == QStyle.PixelMetric.PM_ScrollBarExtent:
            # ``PM_ScrollBarExtent`` controls the overall thickness of the bar.  The
            # proxy draws directly inside the provided geometry so we can return the
            # capsule thickness without applying extra padding.
            return self._TRACK_THICKNESS_PX

        if metric in (
            QStyle.PixelMetric.PM_ScrollBarAddLineExtent,
            QStyle.PixelMetric.PM_ScrollBarSubLineExtent,
        ):
            # Returning zero removes the fixed-size arrow regions entirely, matching the
            # clean Windows 11 presentation.
            return 0

        if metric == QStyle.PixelMetric.PM_ScrollView_ScrollBarSpacing:
            # The default spacing leaves a small gutter between the scroll bar and the
            # viewport.  We collapse it to zero so the groove sits flush with the content.
            return 0

        if metric == QStyle.PixelMetric.PM_ScrollBarSliderMin:
            return self._HANDLE_MIN_LENGTH_PX

        return super().pixelMetric(metric, option, widget)

    def styleHint(self, hint: QStyle.StyleHint, option=None, widget=None, returnData=None):
        """Adjust behaviour hints to complement the custom drawing logic."""

        if hint == QStyle.StyleHint.SH_ScrollBar_Transient:
            # ``0`` disables transient (overlay) mode so Qt keeps the scroll bar visible
            # and reserves layout space for it, which mirrors the native desktop
            # behaviour we are emulating.
            return 0

        if hint == QStyle.StyleHint.SH_ScrollBar_ContextMenu:
            # Returning ``0`` prevents Qt from showing the legacy context menu that
            # exposes arrow controls we no longer draw, keeping the UX consistent.
            return 0

        return super().styleHint(hint, option, widget, returnData)


__all__ = ["CustomScrollBarStyle"]
