"""Widget that keeps the floating player bar anchored over the viewer."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget


class PlayerSurface(QWidget):
    """Keep the floating player bar anchored over the active viewer widget."""

    def __init__(
        self,
        content: QWidget,
        overlay: QWidget,
        parent: QWidget | None = None,
        *,
        margin: int = 48,
    ) -> None:
        super().__init__(parent)
        self._margin = margin
        self._controls_visible = False
        self._content = content
        self._overlay = overlay
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh_controls)
        self._stacked: QStackedWidget | None = (
            content if isinstance(content, QStackedWidget) else None
        )
        self._host_widget: QWidget | None = None
        self._window_host: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        content.setParent(self)
        layout.addWidget(content)

        self._configure_overlay_window()
        self.destroyed.connect(self._overlay.close)

        if self._stacked is not None:
            self._stacked.currentChanged.connect(self._on_stack_changed)

        self._bind_overlay_host()
        self._ensure_window_filter()

    # ------------------------------------------------------------------
    # Overlay visibility management
    # ------------------------------------------------------------------
    def show_controls(self) -> None:
        """Display the floating overlay controls and keep them on top."""

        self._controls_visible = True
        self._bind_overlay_host()
        self._ensure_window_filter()
        self._sync_overlay_parent()
        self._overlay.show()
        self.refresh_controls()
        self.schedule_refresh()

    def hide_controls(self) -> None:
        """Hide the floating overlay controls."""

        self._controls_visible = False
        self._overlay.hide()
        self._refresh_timer.stop()

    def refresh_controls(self) -> None:
        """Force the overlay to realign with the viewer when visible."""

        if not self._controls_visible:
            return
        self._reposition_overlay()
        self._overlay.update()

    def schedule_refresh(self, delay_ms: int = 0) -> None:
        """Queue a deferred refresh to run after layout/paint settles."""

        if not self._controls_visible:
            return
        self._refresh_timer.stop()
        self._refresh_timer.start(max(0, delay_ms))

    # ------------------------------------------------------------------
    # QWidget API
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().resizeEvent(event)
        self.refresh_controls()

    def showEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().showEvent(event)
        self._ensure_window_filter()
        self.refresh_controls()

    def hideEvent(self, event) -> None:  # pragma: no cover - GUI behaviour
        super().hideEvent(event)
        self._overlay.hide()

    def eventFilter(self, obj, event):  # pragma: no cover - GUI behaviour
        if obj is self._host_widget and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
            QEvent.Type.Hide,
        }:
            if event.type() == QEvent.Type.Hide:
                self._overlay.hide()
            else:
                self.schedule_refresh()
        if obj is self._window_host and event.type() in {
            QEvent.Type.Move,
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.WindowStateChange,
        }:
            if self._window_host is not None and self._window_host.isMinimized():
                self._overlay.hide()
            else:
                if self._controls_visible:
                    self._overlay.show()
                self.schedule_refresh()
        if obj is self._window_host and event.type() == QEvent.Type.Hide:
            self._overlay.hide()
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _reposition_overlay(self) -> None:
        if not self._controls_visible:
            return
        host = self._host_widget or self
        rect = host.rect()
        available_width = max(0, rect.width() - (2 * self._margin))
        if available_width == 0 or rect.height() <= 0:
            return
        hint = self._overlay.sizeHint()
        overlay_width = min(hint.width(), available_width)
        overlay_height = hint.height()
        host_origin = host.mapToGlobal(rect.topLeft())
        x = host_origin.x() + (rect.width() - overlay_width) // 2
        y = host_origin.y() + max(0, rect.height() - overlay_height - self._margin)
        self._overlay.setGeometry(x, y, overlay_width, overlay_height)
        self._overlay.raise_()

    def _on_stack_changed(self, _index: int) -> None:
        self._bind_overlay_host()
        self.schedule_refresh()

    def _bind_overlay_host(self) -> None:
        target: QWidget | None = None
        if self._stacked is not None:
            target = self._stacked.currentWidget()
        if target is None:
            target = self._content
        if target is None:
            target = self
        if target is self._host_widget:
            return
        if self._host_widget is not None and self._host_widget is not self:
            self._host_widget.removeEventFilter(self)
        self._host_widget = target
        if self._host_widget is not None and self._host_widget is not self:
            self._host_widget.installEventFilter(self)
        self.schedule_refresh()

    def _ensure_window_filter(self) -> None:
        window = self.window()
        if window is self._window_host:
            return
        if self._window_host is not None:
            self._window_host.removeEventFilter(self)
        self._window_host = window
        if self._window_host is not None:
            self._window_host.installEventFilter(self)

    def _configure_overlay_window(self) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self._overlay.setWindowFlags(flags)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._overlay.hide()

    def _sync_overlay_parent(self) -> None:
        window = self.window()
        if window is self._overlay.parent():
            return
        self._overlay.setParent(window)
        self._configure_overlay_window()
