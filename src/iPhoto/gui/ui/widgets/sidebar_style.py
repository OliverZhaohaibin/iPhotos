"""Custom style tweaks for the album sidebar tree view."""

from __future__ import annotations

from PySide6.QtWidgets import QProxyStyle, QStyle


class SidebarStyle(QProxyStyle):
    """Proxy style stripping row-wide highlights from the decoration gutter."""

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option,
        painter,
        widget=None,
    ):  # type: ignore[override]
        """Avoid painting the default hover/selection background for entire rows."""

        if element == QStyle.PrimitiveElement.PE_PanelItemViewRow:
            return
        return super().drawPrimitive(element, option, painter, widget)

    def styleHint(
        self,
        hint: QStyle.StyleHint,
        option=None,
        widget=None,
        returnData=None,
    ):  # type: ignore[override]
        """Prevent extending selection highlights into the decoration area."""

        if hint == QStyle.StyleHint.SH_ItemView_ShowDecorationSelected:
            return 0
        return super().styleHint(hint, option, widget, returnData)


__all__ = ["SidebarStyle"]
