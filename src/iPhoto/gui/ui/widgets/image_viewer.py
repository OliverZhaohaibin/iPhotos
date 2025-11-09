"""Widget that displays a scaled image while preserving aspect ratio."""

from __future__ import annotations

from typing import Optional, Tuple, cast

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..palette import viewer_surface_color


class ImageViewer(QWidget):
    """Simple viewer that centers, zooms, and scrolls a ``QPixmap``."""

    replayRequested = Signal()
    """Emitted when the user clicks the still frame to replay a Live Photo."""

    zoomChanged = Signal(float)
    """Emitted whenever the zoom factor changes via UI or programmatic control."""

    nextItemRequested = Signal()
    """Emitted when a wheel gesture requests navigation to the next asset."""

    prevItemRequested = Signal()
    """Emitted when a wheel gesture requests navigation to the previous asset."""

    fullscreenExitRequested = Signal()
    """Emitted when a double-click should exit the immersive full screen mode."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Use a ``Fixed`` size policy along both axes so the scroll area honours
        # whichever explicit dimensions we assign to the label.  Allowing Qt to
        # treat the widget as resizable encourages it to stretch the pixmap to
        # fill the viewport, which distorts photos whose aspect ratio differs
        # from the window.
        self._label.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        # ``scaledContents`` defaults to ``False`` but we set it explicitly to
        # document the intent that the pixmap should never be stretched to cover
        # the widget's rectangle.
        self._label.setScaledContents(False)

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(False)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Render the scroll area's viewport with the shared viewer surface colour so
        # the photo canvas aligns with the rest of the interface instead of falling
        # back to a stark pure white.  The neutral tone keeps the media prominent
        # while visually integrating with the surrounding chrome.
        # Query the palette-derived window colour so the viewer canvas perfectly
        # matches the rest of the detail panel instead of relying on a fixed hex
        # value that might drift from the active theme.
        surface_color = viewer_surface_color(self)
        self._default_surface_color = surface_color
        self._surface_override: str | None = None
        self._scroll_area.setStyleSheet(
            f"background-color: {surface_color}; border: none;"
        )
        self._scroll_area.viewport().setStyleSheet(
            f"background-color: {surface_color}; border: none;"
        )
        self._scroll_area.setWidget(self._label)
        self._scroll_area.viewport().installEventFilter(self)

        # ``_loading_overlay`` presents a translucent message while expensive
        # background work (such as decoding or tone-mapping a large image) is
        # in flight.  Painting it as a child widget keeps the implementation
        # simple and avoids introducing additional layout containers around the
        # scroll area while still covering the entire viewer surface.
        self._loading_overlay = QLabel("Loading…", self)
        self._loading_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self._loading_overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 128); color: white; font-size: 18px;"
        )
        self._loading_overlay.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._scroll_area)

        # Mirror the scroll area's backdrop on the container widget to avoid sudden
        # colour changes when layout spacing exposes the parent's palette.
        self.setStyleSheet(f"background-color: {surface_color};")

        self._live_replay_enabled = False
        self._zoom_factor = 1.0
        self._min_zoom = 0.1
        self._max_zoom = 4.0
        self._button_step = 0.1
        self._wheel_step = 0.1
        self._base_size: Optional[QSize] = None
        self._is_panning = False
        self._pan_start_pos = QPoint()
        # ``_wheel_action`` allows the controller to toggle between zooming and delegating the
        # gesture to a parent widget that might interpret the wheel as navigation.
        self._wheel_action = "navigate"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_surface_color_override(self, colour: str | None) -> None:
        """Override the viewer backdrop with *colour* or restore the default."""

        self._surface_override = colour
        target = colour if colour is not None else self._default_surface_color
        stylesheet = f"background-color: {target}; border: none;"
        self._scroll_area.setStyleSheet(stylesheet)
        self._scroll_area.viewport().setStyleSheet(stylesheet)
        self.setStyleSheet(f"background-color: {target};")

    def set_immersive_background(self, immersive: bool) -> None:
        """Toggle a pure black backdrop used when immersive mode is active."""

        self.set_surface_color_override("#000000" if immersive else None)

    def set_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        """Display *pixmap* and update the scaled rendering."""

        self._loading_overlay.hide()
        self._pixmap = pixmap
        if self._pixmap is None or self._pixmap.isNull():
            self._label.clear()
            # Collapse the label to a zero-sized footprint so the scroll area
            # does not retain stale minimum dimensions from the previous image.
            self._label.setFixedSize(0, 0)
            self._base_size = None
            # Notify observers that the zoom factor has reset along with the pixmap
            # content so UI controls (such as sliders) stay in sync with the cleared
            # state.
            self._zoom_factor = 1.0
            self.zoomChanged.emit(self._zoom_factor)
            return

        # Reset the derived rendering state so the next resize cycle recomputes
        # a baseline size for the new pixmap.
        self._base_size = None
        self._zoom_factor = 1.0

        # Clearing the label prevents the previous, potentially undersized pixmap
        # from lingering on screen while we wait for the viewport to settle on its
        # final geometry.
        self._label.clear()

        # Request a fresh layout pass so Qt recalculates the scroll area's viewport
        # using the intrinsic size of the new pixmap.
        self.updateGeometry()

        # Defer the actual rendering until Qt finishes processing the current event
        # queue. By the time this single-shot timer fires, the layout system will have
        # provided a stable viewport size, allowing ``_render_pixmap`` to compute the
        # correct scaling on the first paint.
        QTimer.singleShot(0, self._render_pixmap)
        self.zoomChanged.emit(self._zoom_factor)

    def pixmap(self) -> Optional[QPixmap]:
        """Return a defensive copy of the currently rendered pixmap.

        The edit controller reuses the preview image when leaving the edit view
        so the detail view can display the final adjustments immediately.  A
        copy keeps that hand-off safe even if the caller clears the viewer while
        the pixmap is still referenced elsewhere.
        """

        if self._pixmap is None or self._pixmap.isNull():
            return None
        return QPixmap(self._pixmap)

    def clear(self) -> None:
        """Remove any currently displayed image."""

        self._pixmap = None
        self._label.clear()
        # Reset the label to an empty frame to avoid inherited geometry forcing
        # subsequent renders to occupy an incorrect aspect ratio.
        self._label.setFixedSize(0, 0)
        self._base_size = None
        self._zoom_factor = 1.0
        self.zoomChanged.emit(self._zoom_factor)
        self._loading_overlay.hide()

    def set_loading(self, loading: bool) -> None:
        """Toggle the inline loading indicator on the viewer surface."""

        if loading:
            self._loading_overlay.setGeometry(self.rect())
            self._loading_overlay.show()
            return
        self._loading_overlay.hide()

    def set_wheel_action(self, action: str) -> None:
        """Control how the viewer reacts to wheel gestures.

        Parameters
        ----------
        action:
            Either ``"zoom"`` to keep the existing pinch-to-zoom style behaviour or
            ``"navigate"`` to allow parent widgets to treat the wheel as a next/previous
            request. Any unexpected value falls back to ``"navigate"`` so the UI remains
            predictable even if settings files are edited manually.
        """

        self._wheel_action = "zoom" if action == "zoom" else "navigate"

    def resizeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        """Ensure the loading overlay always covers the full viewer area."""

        super().resizeEvent(event)
        if self._loading_overlay.isVisible():
            self._loading_overlay.setGeometry(self.rect())

    def set_live_replay_enabled(self, enabled: bool) -> None:
        """Allow emitting replay requests when the still frame is shown."""

        self._live_replay_enabled = bool(enabled)

    def set_zoom(self, factor: float, *, anchor: Optional[QPoint] = None) -> None:
        """Set the zoom *factor* relative to the fit-to-window baseline."""

        clamped = max(self._min_zoom, min(self._max_zoom, float(factor)))
        if abs(clamped - self._zoom_factor) < 1e-3:
            return

        anchor_ratios: Optional[Tuple[float, float]] = None
        if (
            anchor is not None
            and self._pixmap is not None
            and not self._pixmap.isNull()
            and self._label.width() > 0
            and self._label.height() > 0
        ):
            anchor_ratios = self._capture_anchor_ratios(anchor)

        self._zoom_factor = clamped
        if self._pixmap is not None and not self._pixmap.isNull():
            self._render_pixmap(anchor_point=anchor, anchor_ratios=anchor_ratios)
        self.zoomChanged.emit(self._zoom_factor)

    def reset_zoom(self) -> None:
        """Return the zoom factor to ``1.0`` (fit to window)."""

        self._zoom_factor = 1.0
        if self._pixmap is not None and not self._pixmap.isNull():
            self._render_pixmap()
        self.zoomChanged.emit(self._zoom_factor)

    def zoom_in(self) -> None:
        """Increase the zoom factor using the standard toolbar step."""

        if self._pixmap is None or self._pixmap.isNull():
            return
        self._step_zoom(self._button_step)

    def zoom_out(self) -> None:
        """Decrease the zoom factor using the standard toolbar step."""

        if self._pixmap is None or self._pixmap.isNull():
            return
        self._step_zoom(-self._button_step)

    def zoom_factor(self) -> float:
        """Return the currently applied zoom factor."""

        return self._zoom_factor

    def viewport_center(self) -> QPoint:
        """Return the centre point of the scroll area's viewport."""

        return self._scroll_area.viewport().rect().center()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # pragma: no cover - GUI behaviour
        """Emit a signal when the viewer is double-clicked to exit immersive mode."""

        if event.button() == Qt.MouseButton.LeftButton:
            self.fullscreenExitRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # ------------------------------------------------------------------
    # QWidget overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._pixmap is not None:
            # During the initial layout pass Qt may deliver resize events while the
            # widget is still negotiating its final viewport size. In that window we
            # simply want to fit the image to whatever size is currently available
            # instead of computing anchor points that assume a stable geometry. By
            # delegating directly to ``_render_pixmap`` we ensure the image is
            # re-rendered with the latest dimensions, preventing the first paint from
            # using stale, undersized measurements.
            self._render_pixmap()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # pragma: no cover - GUI behaviour
        # The event filter coordinates click-versus-drag behaviour on the scroll area's
        # viewport. Calling the base implementation keeps Qt's standard focus handling
        # intact.
        super().mousePressEvent(event)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self._scroll_area.viewport():
            if event.type() == QEvent.Type.Wheel:
                wheel_event = cast(QWheelEvent, event)
                if self._wheel_action != "zoom":
                    # Interpret wheel deltas as navigation requests. The same threshold logic as
                    # the filmstrip is reused so trackpad users who generate pixel deltas still
                    # receive responsive behaviour.
                    delta = wheel_event.angleDelta()
                    step = delta.y() or delta.x()
                    if step == 0:
                        pixel_delta = wheel_event.pixelDelta()
                        step = pixel_delta.y() or pixel_delta.x()
                    if step == 0:
                        return False
                    if step < 0:
                        self.nextItemRequested.emit()
                    else:
                        self.prevItemRequested.emit()
                    wheel_event.accept()
                    return True
                if self._pixmap is None or self._pixmap.isNull():
                    return False
                if self._handle_wheel_event(wheel_event):
                    return True

            if event.type() == QEvent.Type.MouseButtonPress:
                mouse_event = cast(QMouseEvent, event)
                if mouse_event.button() == Qt.MouseButton.LeftButton:
                    self._pan_start_pos = mouse_event.pos()
                    if self._is_scrollable():
                        # Signal to the user that dragging is available when the
                        # image exceeds the viewport by switching to an open hand
                        # cursor, but delay activating panning until movement occurs.
                        self.setCursor(Qt.CursorShape.OpenHandCursor)
                        return True
            elif event.type() == QEvent.Type.MouseMove:
                mouse_event = cast(QMouseEvent, event)
                if (
                    mouse_event.buttons() & Qt.MouseButton.LeftButton
                    and not self._pan_start_pos.isNull()
                ):
                    delta = mouse_event.pos() - self._pan_start_pos
                    if self._is_panning or (self._is_scrollable() and delta.manhattanLength() > 3):
                        if not self._is_panning:
                            # The threshold above ensures we only transition into the
                            # active panning state after a deliberate drag. This avoids
                            # interference with quick clicks that should replay media.
                            self._is_panning = True
                            self.setCursor(Qt.CursorShape.ClosedHandCursor)

                        self._scroll_area.horizontalScrollBar().setValue(
                            self._scroll_area.horizontalScrollBar().value() - delta.x()
                        )
                        self._scroll_area.verticalScrollBar().setValue(
                            self._scroll_area.verticalScrollBar().value() - delta.y()
                        )
                        self._pan_start_pos = mouse_event.pos()
                        return True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                mouse_event = cast(QMouseEvent, event)
                if (
                    mouse_event.button() == Qt.MouseButton.LeftButton
                    and not self._pan_start_pos.isNull()
                ):
                    was_panning = self._is_panning
                    self._is_panning = False
                    self._pan_start_pos = QPoint()
                    self.unsetCursor()

                    if not was_panning and self._live_replay_enabled:
                        # A press followed by a release without movement should still
                        # behave like a click, so we trigger the Live Photo replay now.
                        self.replayRequested.emit()
                    return True
        return super().eventFilter(obj, event)

    def viewport_widget(self) -> QWidget:
        """Expose the scroll area's viewport for higher-level event filters."""

        # The main window installs a keyboard event filter on the viewport so it can
        # intercept navigation shortcuts before the scroll area claims them for
        # focus navigation.  Returning the concrete widget keeps the detail view's
        # shortcut wiring self-documenting while avoiding direct access to private
        # attributes from outside the class.
        return self._scroll_area.viewport()

    def _render_pixmap(
        self,
        *,
        anchor_point: Optional[QPoint] = None,
        anchor_ratios: Optional[Tuple[float, float]] = None,
    ) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            self._label.clear()
            self._label.setFixedSize(0, 0)
            self._base_size = None
            return

        viewport_size = self._scroll_area.viewport().size()
        if not viewport_size.isValid() or viewport_size.isEmpty():
            return

        pix_size = self._pixmap.size()
        if pix_size.isEmpty():
            self._label.clear()
            self._label.setFixedSize(0, 0)
            self._base_size = None
            return

        width_ratio = viewport_size.width() / max(1, pix_size.width())
        height_ratio = viewport_size.height() / max(1, pix_size.height())
        base_scale = min(width_ratio, height_ratio)
        if base_scale <= 0:
            base_scale = 1.0

        fit_width = max(1, int(round(pix_size.width() * base_scale)))
        fit_height = max(1, int(round(pix_size.height() * base_scale)))
        self._base_size = QSize(fit_width, fit_height)

        scale = base_scale * self._zoom_factor
        target_width = max(1, int(round(pix_size.width() * scale)))
        target_height = max(1, int(round(pix_size.height() * scale)))

        scaled = self._pixmap.scaled(
            QSize(target_width, target_height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        # Match the label's geometry exactly to the scaled pixmap so Qt does
        # not perform any additional scaling when laying the widget out inside
        # the scroll area.
        self._label.setFixedSize(scaled.size())

        h_bar = self._scroll_area.horizontalScrollBar()
        v_bar = self._scroll_area.verticalScrollBar()
        if anchor_point is not None and anchor_ratios is not None:
            self._restore_anchor(anchor_point, anchor_ratios, h_bar, v_bar)
        else:
            self._center_viewport(h_bar, v_bar)

    def _capture_anchor_ratios(self, anchor: QPoint) -> Tuple[float, float]:
        h_bar = self._scroll_area.horizontalScrollBar()
        v_bar = self._scroll_area.verticalScrollBar()
        content_width = max(1, self._label.width())
        content_height = max(1, self._label.height())
        rel_x = (h_bar.value() + anchor.x()) / content_width
        rel_y = (v_bar.value() + anchor.y()) / content_height
        return (
            max(0.0, min(rel_x, 1.0)),
            max(0.0, min(rel_y, 1.0)),
        )

    def _restore_anchor(
        self,
        anchor_point: QPoint,
        anchor_ratios: Tuple[float, float],
        h_bar: QScrollBar,
        v_bar: QScrollBar,
    ) -> None:
        rel_x, rel_y = anchor_ratios
        content_width = max(1, self._label.width())
        content_height = max(1, self._label.height())

        target_x = int(round(rel_x * content_width - anchor_point.x()))
        target_y = int(round(rel_y * content_height - anchor_point.y()))

        h_bar.setValue(max(h_bar.minimum(), min(target_x, h_bar.maximum())))
        v_bar.setValue(max(v_bar.minimum(), min(target_y, v_bar.maximum())))

    def _center_viewport(self, h_bar: QScrollBar, v_bar: QScrollBar) -> None:
        for bar in (h_bar, v_bar):
            span = bar.maximum() - bar.minimum()
            if span > 0:
                bar.setValue(bar.minimum() + span // 2)
            else:
                bar.setValue(bar.minimum())

    def _handle_wheel_event(self, event: QWheelEvent) -> bool:
        angle = event.angleDelta().y()
        if angle == 0:
            return False

        step = self._wheel_step if angle > 0 else -self._wheel_step
        anchor = event.position().toPoint()
        self.set_zoom(self._zoom_factor + step, anchor=anchor)
        event.accept()
        return True

    def _step_zoom(self, delta: float) -> None:
        anchor = self.viewport_center()
        self.set_zoom(self._zoom_factor + delta, anchor=anchor)

    def _is_scrollable(self) -> bool:
        """Return ``True`` when the current pixmap exceeds the viewport."""

        h_bar = self._scroll_area.horizontalScrollBar()
        v_bar = self._scroll_area.verticalScrollBar()
        # At least one scrollbar must offer a scrollable range (max > min) for the
        # image to be considered pannable.
        return (h_bar.maximum() > h_bar.minimum()) or (v_bar.maximum() > v_bar.minimum())
