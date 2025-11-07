"""Utilities for keeping the detail page widgets in sync with the playlist."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QModelIndex,
    QItemSelectionModel,
    QObject,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QSlider, QToolButton, QWidget

ZOOM_SLIDER_DEFAULT = 100
"""Default percentage value used when the zoom slider is reset."""

from ..icons import load_icon
from ..models.asset_model import AssetModel, Roles
from ..ui_main_window import ChromeStatusBar
from ..widgets.asset_grid import AssetGrid
from ..widgets.info_panel import InfoPanel
from ..widgets.player_bar import PlayerBar
from ..widgets.gl_image_viewer import GLImageViewer
from .header_controller import HeaderController
from .player_view_controller import PlayerViewController
from .view_controller import ViewController
from ....io.metadata import read_image_meta
from ....utils.logging import get_logger


_LOGGER = get_logger()
"""Module level logger used for debug and fallback metadata warnings."""


class DetailUIController(QObject):
    """Manage the collection of widgets that form the detail page."""

    scrubbingStarted = Signal()
    """Emitted when the player bar begins a scrub gesture."""

    scrubbingFinished = Signal()
    """Emitted when the player bar completes a scrub gesture."""

    def __init__(
        self,
        model: AssetModel,
        filmstrip_view: AssetGrid,
        player_view: PlayerViewController,
        player_bar: PlayerBar,
        view_controller: ViewController,
        header: HeaderController,
        favorite_button: QToolButton,
        edit_button: QToolButton,
        info_button: QToolButton,
        info_panel: InfoPanel,
        zoom_widget: QWidget,
        zoom_slider: QSlider,
        zoom_in_button: QToolButton,
        zoom_out_button: QToolButton,
        status_bar: ChromeStatusBar,
        parent: QObject | None = None,
    ) -> None:
        """Store widget references and apply the initial UI baseline."""

        super().__init__(parent)
        self._model = model
        self._filmstrip_view = filmstrip_view
        self._player_view = player_view
        self._player_bar = player_bar
        self._view_controller = view_controller
        self._header = header
        self._favorite_button = favorite_button
        self._edit_button = edit_button
        self._info_button = info_button
        self._info_panel = info_panel
        self._zoom_widget = zoom_widget
        self._zoom_slider = zoom_slider
        self._zoom_in_button = zoom_in_button
        self._zoom_out_button = zoom_out_button
        self._status_bar = status_bar
        self._current_row: int = -1
        self._cached_info: Optional[dict[str, object]] = None
        self._toolbar_icon_tint: str | None = None

        self._initialize_static_state()
        self._wire_player_bar_events()
        self.connect_zoom_controls()
        self._info_button.clicked.connect(self._handle_info_button_clicked)
        self._view_controller.galleryViewShown.connect(self._handle_gallery_view_shown)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def handle_playlist_current_changed(self, current_row: int, previous_row: int) -> None:
        """Synchronise the detail UI when the playlist focus changes."""

        self._current_row = current_row
        self.update_favorite_button(current_row)
        self.update_edit_button(current_row)
        self._update_info_button_state(current_row)
        self._refresh_info_panel(current_row)

        selection_model = self._filmstrip_view.selectionModel()
        if selection_model is None:
            return
        filmstrip_model = self._filmstrip_view.model()
        if filmstrip_model is None:
            return

        source_model = self._model.source_model()

        def _set_is_current(proxy_row: int, value: bool) -> None:
            if proxy_row < 0:
                return
            proxy_index = self._model.index(proxy_row, 0)
            if not proxy_index.isValid():
                return
            source_index = self._model.mapToSource(proxy_index)
            if not source_index.isValid():
                return
            source_model.setData(source_index, value, Roles.IS_CURRENT)

        _set_is_current(previous_row, False)
        if current_row >= 0:
            _set_is_current(current_row, True)

        self._header.update_for_row(current_row if current_row >= 0 else None, self._model)

        proxy_index: Optional[QModelIndex] = None
        if current_row >= 0:
            proxy_row = current_row + 1
            candidate = filmstrip_model.index(proxy_row, 0)
            if candidate.isValid():
                proxy_index = candidate

        self._filmstrip_view.refresh_spacers(proxy_index)
        if current_row < 0:
            self._player_bar.setEnabled(False)
            self._player_view.show_placeholder()
            self.hide_zoom_controls()
            selection_model.clearSelection()
            return

        selection_model.clearSelection()
        if proxy_index is None:
            return
        selection_model.select(
            proxy_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(
            proxy_index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        self._filmstrip_view.refresh_spacers(proxy_index)
        QTimer.singleShot(0, lambda idx=proxy_index: self._filmstrip_view.center_on_index(idx))
        self._player_bar.setEnabled(True)
        self._view_controller.show_detail_view()
        self._filmstrip_view.doItemsLayout()

        index = self._model.index(current_row, 0)
        if index.isValid() and bool(index.data(Roles.IS_IMAGE)):
            self.show_zoom_controls()
        else:
            self.hide_zoom_controls()

    def update_favorite_button(
        self, row: int, *, is_featured: Optional[bool] = None
    ) -> None:
        """Reflect the featured state of *row* on the favorite button."""

        if row < 0:
            self._favorite_button.setEnabled(False)
            self._favorite_button.setIcon(self._load_toolbar_icon("suit.heart.svg"))
            self._favorite_button.setToolTip("Add to Favorites")
            return

        index = self._model.index(row, 0)
        if not index.isValid():
            self._favorite_button.setEnabled(False)
            self._favorite_button.setIcon(self._load_toolbar_icon("suit.heart.svg"))
            self._favorite_button.setToolTip("Add to Favorites")
            return

        featured_state = is_featured
        if featured_state is None:
            featured_state = bool(index.data(Roles.FEATURED))

        icon_name = "suit.heart.fill.svg" if featured_state else "suit.heart.svg"
        tooltip = "Remove from Favorites" if featured_state else "Add to Favorites"
        self._favorite_button.setEnabled(True)
        self._favorite_button.setIcon(self._load_toolbar_icon(icon_name))
        self._favorite_button.setToolTip(tooltip)

    def update_edit_button(self, row: int) -> None:
        """Enable or disable the edit button depending on asset type."""

        if row < 0:
            self._edit_button.setEnabled(False)
            return

        index = self._model.index(row, 0)
        if not index.isValid():
            self._edit_button.setEnabled(False)
            return

        is_image = bool(index.data(Roles.IS_IMAGE))
        self._edit_button.setEnabled(is_image)

    def set_toolbar_icon_tint(self, color: QColor | str | None) -> None:
        """Tint the info and favorite toolbar icons using *color* when provided.

        The edit controller activates a dark theme for the shared toolbar buttons
        while edit mode is active.  Accepting both :class:`~PySide6.QtGui.QColor`
        and string inputs keeps the API flexible and lets the controller pass the
        exact colour already computed for the rest of the chrome.  Passing
        ``None`` clears the tint so the light theme icons are reinstated when the
        user exits edit mode.
        """

        if color is None:
            tint = None
        elif isinstance(color, QColor):
            tint = color.name(QColor.NameFormat.HexArgb)
        else:
            parsed = QColor(color)
            tint = parsed.name(QColor.NameFormat.HexArgb) if parsed.isValid() else None

        if tint == self._toolbar_icon_tint:
            return

        self._toolbar_icon_tint = tint
        self._refresh_toolbar_icons()

    def _refresh_toolbar_icons(self) -> None:
        """Reapply toolbar icons so they honour the current tint selection."""

        # The info button keeps a fixed glyph, so update it directly.
        self._info_button.setIcon(self._load_toolbar_icon("info.circle.svg"))
        # The favourite button icon depends on the active row; reusing the public
        # helper ensures we respect both the tint and the featured state.
        self.update_favorite_button(self._current_row)

    def _load_toolbar_icon(self, name: str) -> QIcon:
        """Return *name* tinted with the current toolbar colour, if any."""

        if self._toolbar_icon_tint is None:
            return load_icon(name)
        return load_icon(name, color=self._toolbar_icon_tint)

    def update_header(self, row: Optional[int]) -> None:
        """Update the header metadata for *row*."""

        self._header.update_for_row(row if row is not None else None, self._model)

    def show_detail_view(self) -> None:
        """Ensure the detail page is visible."""

        self._view_controller.show_detail_view()

    def show_placeholder(self) -> None:
        """Swap the player area back to its placeholder state."""

        self._player_view.show_placeholder()
        self._player_bar.setEnabled(False)
        self.hide_zoom_controls()
        self._current_row = -1
        self._cached_info = None
        self._info_button.setEnabled(False)
        if self._info_panel.isVisible():
            self._info_panel.close()

    def show_live_badge(self) -> None:
        """Expose a convenient wrapper for displaying the Live Photo badge."""

        self._player_view.show_live_badge()

    def hide_live_badge(self) -> None:
        """Hide the Live Photo badge."""

        self._player_view.hide_live_badge()

    def set_live_replay_enabled(self, enabled: bool) -> None:
        """Toggle the Live Photo replay affordance."""

        self._player_view.set_live_replay_enabled(enabled)

    def select_filmstrip_row(self, row: int) -> None:
        """Select *row* in the filmstrip view if it exists."""

        selection_model = self._filmstrip_view.selectionModel()
        if selection_model is None or row < 0:
            return
        filmstrip_model = self._filmstrip_view.model()
        if filmstrip_model is None:
            return
        proxy_row = row + 1
        proxy_index = filmstrip_model.index(proxy_row, 0)
        if not proxy_index.isValid():
            return
        selection_model.setCurrentIndex(
            proxy_index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        selection_model.select(
            proxy_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._filmstrip_view.refresh_spacers(proxy_index)
        QTimer.singleShot(0, lambda idx=proxy_index: self._filmstrip_view.center_on_index(idx))

    def reset_for_gallery_view(self) -> None:
        """Clear detail-specific UI when switching back to the gallery."""

        self._player_bar.setEnabled(False)
        self._player_view.show_placeholder()
        self._header.clear()
        self.update_favorite_button(-1)
        self._filmstrip_view.clearSelection()
        self._status_bar.showMessage("Browse your library")
        self.hide_zoom_controls()

    def show_status_message(self, message: str) -> None:
        """Mirror *message* to the status bar."""

        self._status_bar.showMessage(message)

    def set_player_bar_enabled(self, enabled: bool) -> None:
        """Toggle the enabled state of the player bar."""

        self._player_bar.setEnabled(enabled)

    def reset_player_bar(self) -> None:
        """Reset the player bar to its default position and duration."""

        self._player_bar.reset()

    def set_player_position(self, position_ms: int) -> None:
        """Mirror the current playback position onto the player bar."""

        self._player_bar.set_position(position_ms)

    def set_player_duration(self, duration_ms: int) -> None:
        """Update the player bar with the latest media duration."""

        self._player_bar.set_duration(duration_ms)

    def set_playback_state(self, state: object) -> None:
        """Synchronise the player bar's play/pause affordance."""

        self._player_bar.set_playback_state(state)

    def is_player_at_end(self) -> bool:
        """Return ``True`` when the slider reports that playback has ended."""

        duration = self._player_bar.duration()
        if duration <= 0:
            return False
        return self._player_bar.position() >= duration

    def player_duration(self) -> int:
        """Expose the current duration tracked by the player bar."""

        return self._player_bar.duration()

    def set_player_position_to_start(self) -> None:
        """Reset the player position to the first frame."""

        self._player_bar.set_position(0)

    @property
    def player_bar(self) -> PlayerBar:
        """Expose the managed player bar instance."""

        return self._player_bar

    @property
    def player_view(self) -> PlayerViewController:
        """Expose the managed player view controller."""

        return self._player_view

    @property
    def filmstrip_view(self) -> AssetGrid:
        """Expose the managed filmstrip view."""

        return self._filmstrip_view

    @property
    def favorite_button(self) -> QToolButton:
        """Expose the favorite button so higher-level controllers can wire slots."""

        return self._favorite_button

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _wire_player_bar_events(self) -> None:
        """Translate player bar gestures into high-level controller signals."""

        self._player_bar.scrubStarted.connect(self._on_player_bar_scrub_started)
        self._player_bar.scrubFinished.connect(self._on_player_bar_scrub_finished)

    @Slot()
    def _on_player_bar_scrub_started(self) -> None:
        """Forward scrub start events through :attr:`scrubbingStarted`."""

        self.scrubbingStarted.emit()

    @Slot()
    def _on_player_bar_scrub_finished(self) -> None:
        """Forward scrub completion events through :attr:`scrubbingFinished`."""

        self.scrubbingFinished.emit()

    def _initialize_static_state(self) -> None:
        """Apply the default state expected when the controller is created."""

        self._header.clear()
        self._player_view.hide_live_badge()
        self._player_view.set_live_replay_enabled(False)
        self._favorite_button.setEnabled(False)
        # Use the helper so the icon respects any active dark-mode tint.
        self._favorite_button.setIcon(self._load_toolbar_icon("suit.heart.svg"))
        self._favorite_button.setToolTip("Add to Favorites")
        self._edit_button.setEnabled(False)
        self._edit_button.setIcon(load_icon("slider.horizontal.3.svg"))
        self._edit_button.setToolTip("Edit adjustments")
        self._info_button.setEnabled(False)
        self._info_button.setIcon(self._load_toolbar_icon("info.circle.svg"))
        self.hide_zoom_controls()

    def _update_info_button_state(self, row: int) -> None:
        """Enable the info button whenever the selection is an image or a video."""

        if row < 0:
            self._info_button.setEnabled(False)
            if self._info_panel.isVisible():
                self._info_panel.close()
            return

        index = self._model.index(row, 0)
        if not index.isValid():
            self._info_button.setEnabled(False)
            if self._info_panel.isVisible():
                self._info_panel.close()
            return

        is_image = bool(index.data(Roles.IS_IMAGE))
        is_video = bool(index.data(Roles.IS_VIDEO))
        self._info_button.setEnabled(is_image or is_video)
        if not (is_image or is_video) and self._info_panel.isVisible():
            self._info_panel.close()

    def _refresh_info_panel(self, row: int) -> None:
        """Update the cached metadata and the floating panel for *row*."""

        if row < 0:
            self._cached_info = None
            if self._info_panel.isVisible():
                self._info_panel.clear()
            return

        index = self._model.index(row, 0)
        if not index.isValid():
            self._cached_info = None
            if self._info_panel.isVisible():
                self._info_panel.clear()
            return

        is_image = bool(index.data(Roles.IS_IMAGE))
        is_video = bool(index.data(Roles.IS_VIDEO))
        if not (is_image or is_video):
            self._cached_info = None
            if self._info_panel.isVisible():
                self._info_panel.clear()
            return

        raw_info = index.data(Roles.INFO)
        if not isinstance(raw_info, dict):
            self._cached_info = None
            if self._info_panel.isVisible():
                self._info_panel.clear()
            return

        payload = dict(raw_info)
        rel_value = payload.get("rel")
        if not isinstance(rel_value, str) or not rel_value:
            rel_candidate = index.data(Roles.REL)
            if isinstance(rel_candidate, str) and rel_candidate:
                payload["rel"] = rel_candidate
        payload.setdefault("is_image", is_image)
        payload.setdefault("is_video", is_video)
        payload = self._enrich_metadata_if_needed(payload, index)
        self._cached_info = payload
        if self._info_panel.isVisible():
            self._info_panel.set_asset_metadata(self._cached_info)

    def _handle_info_button_clicked(self) -> None:
        """Show or hide the info panel for the current playlist row."""

        if self._current_row < 0:
            return
        if not self._cached_info:
            self._refresh_info_panel(self._current_row)
        if not self._cached_info:
            if self._status_bar is not None:
                self._status_bar.showMessage(
                    "No metadata is available for this asset.",
                    3000,
                )
            return

        current_rel = self._cached_info.get("rel")
        if (
            self._info_panel.isVisible()
            and isinstance(current_rel, str)
            and current_rel
            and self._info_panel.current_rel() == current_rel
        ):
            self._info_panel.close()
            return

        self._info_panel.set_asset_metadata(self._cached_info)
        self._info_panel.show()
        self._info_panel.raise_()
        self._info_panel.activateWindow()

    def _handle_gallery_view_shown(self) -> None:
        """Ensure the floating info panel hides when returning to the gallery."""

        if self._info_panel.isVisible():
            self._info_panel.close()

    def _enrich_metadata_if_needed(
        self, payload: dict[str, object], index: QModelIndex
    ) -> dict[str, object]:
        """Populate exposure related fields when the cached payload lacks them.

        Older ``index.jsonl`` snapshots may have been generated before the
        enriched EXIF pipeline existed.  In that scenario the info panel would
        permanently show the fallback "no metadata" message.  This helper keeps
        the UI responsive by reading the metadata directly from disk when the
        cached payload offers no useful exposure information.
        """

        exposure_keys = (
            "iso",
            "f_number",
            "exposure_time",
            "exposure_compensation",
            "focal_length",
        )

        if not bool(index.data(Roles.IS_IMAGE)):
            return payload

        # Bail out quickly when at least one exposure value is present.  This
        # ensures the happy path remains inexpensive and we avoid touching the
        # filesystem for every selection change in the filmstrip.
        if any(payload.get(key) not in (None, "") for key in exposure_keys):
            return payload

        abs_path = payload.get("abs")
        if not isinstance(abs_path, str) or not abs_path:
            # Older index rows may not include an absolute path, so fall back to
            # the model role to recover it and remember the resolved path for
            # future lookups.
            abs_candidate = index.data(Roles.ABS)
            if isinstance(abs_candidate, str) and abs_candidate:
                abs_path = abs_candidate
                payload.setdefault("abs", abs_path)

        if not isinstance(abs_path, str) or not abs_path:
            return payload

        try:
            fresh_meta = read_image_meta(Path(abs_path))
        except Exception as exc:  # pragma: no cover - defensive fallback
            _LOGGER.debug("Failed to enrich metadata for %s: %s", abs_path, exc)
            return payload

        # Copy the newly parsed exposure values into the cached payload so the
        # info panel can render the detailed line without requiring a rescan.
        for key in exposure_keys:
            value = fresh_meta.get(key)
            if value not in (None, ""):
                payload[key] = value

        # Supplementary EXIF fields improve the camera and lens sections; keep
        # the existing cached values when they already exist.
        for ancillary_key in ("lens", "make", "model"):
            value = fresh_meta.get(ancillary_key)
            if value not in (None, ""):
                payload.setdefault(ancillary_key, value)

        # Mirror width/height/file size when the cached payload has gaps so the
        # summary line can show dimensions and an accurate file size.
        if "w" not in payload or payload.get("w") in (None, 0):
            width = fresh_meta.get("w")
            if isinstance(width, int) and width > 0:
                payload["w"] = width
        if "h" not in payload or payload.get("h") in (None, 0):
            height = fresh_meta.get("h")
            if isinstance(height, int) and height > 0:
                payload["h"] = height

        if payload.get("bytes") in (None, 0):
            size = fresh_meta.get("bytes")
            if isinstance(size, (int, float)) and size > 0:
                payload["bytes"] = size

        return payload

    def connect_zoom_controls(self) -> None:
        """Connect the zoom toolbar to the detail image viewer."""

        viewer = self._player_view.image_viewer
        self._zoom_in_button.clicked.connect(viewer.zoom_in)
        self._zoom_out_button.clicked.connect(viewer.zoom_out)
        self._zoom_slider.valueChanged.connect(self._handle_zoom_slider_changed)
        viewer.zoomChanged.connect(self._handle_viewer_zoom_changed)

    def disconnect_zoom_controls(self) -> None:
        """Disconnect the zoom toolbar from the detail image viewer safely."""

        viewer = self._player_view.image_viewer
        try:
            self._zoom_in_button.clicked.disconnect(viewer.zoom_in)
            self._zoom_out_button.clicked.disconnect(viewer.zoom_out)
            self._zoom_slider.valueChanged.disconnect(self._handle_zoom_slider_changed)
            viewer.zoomChanged.disconnect(self._handle_viewer_zoom_changed)
        except (RuntimeError, TypeError):
            # Qt raises ``RuntimeError``/``TypeError`` when a signal is already disconnected or was
            # never linked.  The edit view frequently borrows the toolbar, so suppress these
            # harmless consistency errors.
            pass

    def _handle_zoom_slider_changed(self, value: int) -> None:
        """Translate slider values into viewer zoom factors."""

        viewer = self._player_view.image_viewer
        target = self._slider_value_to_zoom(value)
        viewer.set_zoom(target, anchor=viewer.viewport_center())

    def _handle_viewer_zoom_changed(self, factor: float) -> None:
        """Synchronise the slider position with the viewer's zoom factor."""

        slider_value = self._zoom_to_slider_value(factor)
        if slider_value == self._zoom_slider.value():
            return
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(slider_value)
        self._zoom_slider.blockSignals(False)

    def _slider_value_to_zoom(self, value: int) -> float:
        """Convert the slider *value* (percent) into a zoom factor."""

        clamped = max(self._zoom_slider.minimum(), min(self._zoom_slider.maximum(), value))
        return float(clamped) / 100.0

    def _zoom_to_slider_value(self, factor: float) -> int:
        """Convert a zoom *factor* into the slider percentage domain."""

        scaled = int(round(factor * 100))
        return max(self._zoom_slider.minimum(), min(self._zoom_slider.maximum(), scaled))

    def show_zoom_controls(self) -> None:
        """Make the zoom toolbar visible."""

        self._zoom_widget.show()

    def hide_zoom_controls(self) -> None:
        """Hide the zoom toolbar and reset it to the default position."""

        self._zoom_widget.hide()
        self._reset_zoom_slider()

    def _reset_zoom_slider(self) -> None:
        """Return the slider to its default percentage without emitting signals."""

        default_value = max(
            self._zoom_slider.minimum(),
            min(self._zoom_slider.maximum(), ZOOM_SLIDER_DEFAULT),
        )
        if self._zoom_slider.value() == default_value:
            return
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(default_value)
        self._zoom_slider.blockSignals(False)

    @property
    def status_bar(self) -> ChromeStatusBar:
        """Provide access to the managed status bar."""

        return self._status_bar
