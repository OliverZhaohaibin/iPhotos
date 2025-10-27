"""Helpers for building shared Qt stylesheet snippets."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

# The tooltip rules remain simple enough to ship as a constant, but we wrap them in a helper
# below so callers can opt-in without importing the string directly.
_TOOLTIP_STYLES = """
QToolTip {
    color: palette(text);
    background-color: palette(base);
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 4px;
    opacity: 230;
}
""".strip()


def ensure_opaque_color(color: QColor) -> QColor:
    """Return *color* with its alpha channel forced to full opacity."""

    if color.alpha() >= 255:
        return color

    resolved = QColor(color)
    resolved.setAlpha(255)
    return resolved


def build_menu_styles(palette: QPalette) -> tuple[str, str]:
    """Return palette-aware styles for ``QMenu`` and ``QMenuBar`` widgets."""

    window_color = ensure_opaque_color(palette.color(QPalette.ColorRole.Window))
    border_color = ensure_opaque_color(palette.color(QPalette.ColorRole.Mid))
    text_color = ensure_opaque_color(palette.color(QPalette.ColorRole.WindowText))
    highlight_color = ensure_opaque_color(palette.color(QPalette.ColorRole.Highlight))
    highlight_text_color = ensure_opaque_color(
        palette.color(QPalette.ColorRole.HighlightedText)
    )
    separator_color = ensure_opaque_color(palette.color(QPalette.ColorRole.Midlight))

    window_color_name = window_color.name()
    border_color_name = border_color.name()
    text_color_name = text_color.name()
    highlight_color_name = highlight_color.name()
    highlight_text_color_name = highlight_text_color.name()
    separator_color_name = separator_color.name()

    border_radius_px = 10
    item_radius_px = 6

    qmenu_style = (
        "QMenu {\n"
        f"    background-color: {window_color_name};\n"
        f"    border: 1px solid {border_color_name};\n"
        f"    border-radius: {border_radius_px}px;\n"
        "    padding: 4px;\n"
        "    margin: 0px;\n"
        "}\n"
        "QMenu::item {\n"
        "    background-color: transparent;\n"
        f"    color: {text_color_name};\n"
        "    padding: 5px 20px;\n"
        "    margin: 2px 6px;\n"
        f"    border-radius: {item_radius_px}px;\n"
        "}\n"
        "QMenu::item:selected {\n"
        f"    background-color: {highlight_color_name};\n"
        f"    color: {highlight_text_color_name};\n"
        "}\n"
        "QMenu::separator {\n"
        "    height: 1px;\n"
        f"    background: {separator_color_name};\n"
        "    margin: 4px 10px;\n"
        "}"
    )

    menubar_style = (
        "QMenuBar {\n"
        f"    background-color: {window_color_name};\n"
        "    border-radius: 0px;\n"
        "    padding: 2px;\n"
        "}\n"
        "QMenuBar::item {\n"
        "    background-color: transparent;\n"
        f"    color: {text_color_name};\n"
        "    padding: 4px 10px;\n"
        "    border-radius: 4px;\n"
        "}\n"
        "QMenuBar::item:selected {\n"
        f"    background-color: {highlight_color_name};\n"
        f"    color: {highlight_text_color_name};\n"
        "}\n"
        "QMenuBar::separator {\n"
        f"    background: {separator_color_name};\n"
        "    width: 1px;\n"
        "    margin: 4px 2px;\n"
        "}"
    )

    return qmenu_style, menubar_style


def build_scrollbar_styles(palette: QPalette) -> str:
    """Construct palette-aware rounded scrollbars for both orientations."""

    background_colour = ensure_opaque_color(palette.color(QPalette.ColorRole.Window))
    window_lightness = background_colour.lightness()
    if window_lightness < 128:
        base_value = 190
        hover_value = 215
        pressed_value = 165
    else:
        base_value = 154
        hover_value = 127
        pressed_value = 106

    handle_colour = QColor(base_value, base_value, base_value)
    hover_colour = QColor(hover_value, hover_value, hover_value)
    pressed_colour = QColor(pressed_value, pressed_value, pressed_value)

    groove_colour = QColor(background_colour)
    if window_lightness < 128:
        groove_colour = groove_colour.lighter(140)
    else:
        groove_colour = groove_colour.darker(115)
    groove_colour.setAlpha(120)

    groove_red, groove_green, groove_blue, groove_alpha = groove_colour.getRgb()

    thickness_px = 10
    corner_radius_px = 4
    minimum_handle_length_px = 25

    background_colour_name = background_colour.name()
    handle_colour_name = handle_colour.name()
    hover_colour_name = hover_colour.name()
    pressed_colour_name = pressed_colour.name()
    groove_colour_name = (
        f"rgba({groove_red}, {groove_green}, {groove_blue}, {groove_alpha})"
    )

    scrollbar_style = (
        "QScrollBar {\n"
        "    border: none;\n"
        "    background: transparent;\n"
        "}\n"
        "QScrollBar:vertical {\n"
        f"    width: {thickness_px}px;\n"
        "    margin: 1px 1px 1px 1px;\n"
        f"    background-color: {background_colour_name};\n"
        "}\n"
        "QScrollBar:horizontal {\n"
        f"    height: {thickness_px}px;\n"
        "    margin: 1px 1px 1px 1px;\n"
        f"    background-color: {background_colour_name};\n"
        "}\n"
        "QScrollBar::handle {\n"
        f"    background-color: {handle_colour_name};\n"
        "    border-radius: 4px;\n"
        "    border: none;\n"
        "}\n"
        "QScrollBar::handle:vertical {\n"
        f"    min-height: {minimum_handle_length_px}px;\n"
        "    margin: 0px 1px 0px 1px;\n"
        "}\n"
        "QScrollBar::handle:horizontal {\n"
        f"    min-width: {minimum_handle_length_px}px;\n"
        "    margin: 1px 0px 1px 0px;\n"
        "}\n"
        "QScrollBar::handle:hover {\n"
        f"    background-color: {hover_colour_name};\n"
        "}\n"
        "QScrollBar::handle:pressed {\n"
        f"    background-color: {pressed_colour_name};\n"
        "}\n"
        "QScrollBar::add-line, QScrollBar::sub-line {\n"
        "    height: 0px;\n"
        "    width: 0px;\n"
        "    border: none;\n"
        "    background: none;\n"
        "    subcontrol-position: none;\n"
        "    subcontrol-origin: margin;\n"
        "}\n"
        "QScrollBar::up-arrow, QScrollBar::down-arrow,\n"
        "QScrollBar::left-arrow, QScrollBar::right-arrow {\n"
        "    height: 0px;\n"
        "    width: 0px;\n"
        "    background: none;\n"
        "}\n"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,\n"
        "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {\n"
        f"    min-height: {minimum_handle_length_px}px;\n"
        f"    min-width: {minimum_handle_length_px}px;\n"
        "}\n"
        "QScrollBar::groove:vertical, QScrollBar::groove:horizontal {\n"
        f"    background-color: {groove_colour_name};\n"
        f"    border-radius: {corner_radius_px}px;\n"
        "    margin: 1px;\n"
        "}\n"
    )

    return scrollbar_style


def build_tooltip_styles() -> str:
    """Return the stylesheet block that standardises tooltip chrome."""

    return _TOOLTIP_STYLES


def build_global_stylesheet(palette: QPalette) -> str:
    """Combine every shared stylesheet block into a single string."""

    qmenu_style, menubar_style = build_menu_styles(palette)
    parts = [
        build_scrollbar_styles(palette),
        build_tooltip_styles(),
        qmenu_style,
        menubar_style,
    ]
    return "\n".join(part for part in parts if part)


__all__ = [
    "ensure_opaque_color",
    "build_menu_styles",
    "build_scrollbar_styles",
    "build_tooltip_styles",
    "build_global_stylesheet",
]
