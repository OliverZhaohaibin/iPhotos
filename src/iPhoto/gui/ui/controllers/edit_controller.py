"""Controller coordinating the edit view and non-destructive adjustments."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget

from ....io import sidecar
from ...utils import image_loader
from ..models.asset_model import AssetModel
from ..models.edit_session import EditSession
from ..tasks.thumbnail_loader import ThumbnailLoader
from ..ui_main_window import Ui_MainWindow
from .edit_preview_manager import EditPreviewManager
from .edit_view_transition import EditViewTransitionManager
from .player_view_controller import PlayerViewController
from .view_controller import ViewController

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from .detail_ui_controller import DetailUIController
    from .navigation_controller import NavigationController


_LOGGER = logging.getLogger(__name__)


class EditController(QObject):
    """Own the edit session state and synchronise UI widgets."""

    editingStarted = Signal(Path)
    """Emitted after a source asset has been loaded for editing."""

    editingFinished = Signal(Path)
    """Emitted once adjustments are saved and the detail view is restored."""

    def __init__(
        self,
        ui: Ui_MainWindow,
        view_controller: ViewController,
        player_view: PlayerViewController,
        playlist,
        asset_model: AssetModel,
        parent: Optional[QObject] = None,
        *,
        navigation: "NavigationController" | None = None,
        detail_ui_controller: "DetailUIController" | None = None,
    ) -> None:
        super().__init__(parent)
        # ``parent`` is the main window hosting the edit UI.  Retaining a weak reference to the
        # window allows the controller to ask the frameless window manager to rebuild menu styles
        # after the palette flips between light and dark variants.
        self._window: QObject | None = parent
        self._ui = ui
        self._view_controller = view_controller
        self._player_view = player_view
        self._playlist = playlist
        self._asset_model = asset_model
        # ``_navigation`` is injected lazily so the controller can coordinate
        # with :class:`NavigationController` without creating an import cycle
        # during startup.  The reference stays optional because unit tests may
        # exercise the edit workflow without bootstrapping the full GUI stack.
        self._navigation: "NavigationController" | None = navigation
        # ``_detail_ui_controller`` provides access to the detail view's zoom wiring helpers so the
        # shared zoom toolbar can swap targets cleanly when the edit tools take over the header.
        self._detail_ui_controller: "DetailUIController" | None = detail_ui_controller
        # Track whether the shared zoom controls are currently routed to the edit viewer so we can
        # disconnect them without relying on Qt to silently drop redundant requests.  Qt logs a
        # warning when asked to disconnect a link that was never created, so this boolean keeps the
        # console clean while still allowing repeated hand-overs between the detail and edit views.
        self._edit_zoom_controls_connected = False
        self._thumbnail_loader: ThumbnailLoader = asset_model.thumbnail_loader()
        # ``_pending_thumbnail_refreshes`` tracks relative asset identifiers with
        # a refresh queued via :meth:`_schedule_thumbnail_refresh`.  Deferring
        # the cache invalidation keeps the detail pane visible when edits are
        # saved, preventing the grid view from temporarily reclaiming focus just
        # so it can reload its thumbnails.
        self._pending_thumbnail_refreshes: set[str] = set()

        self._preview_manager = EditPreviewManager(self._ui.edit_image_viewer, self)
        self._preview_manager.preview_updated.connect(self._on_preview_pixmap_updated)
        self._preview_manager.image_cleared.connect(self._ui.edit_image_viewer.clear)

        self._transition_manager = EditViewTransitionManager(self._ui, self._window, self)
        self._transition_manager.transition_finished.connect(self._on_transition_finished)
        self._transition_manager.set_detail_ui_controller(self._detail_ui_controller)

        self._session: Optional[EditSession] = None
        self._current_source: Optional[Path] = None
        self._compare_active = False
        # ``_edit_viewer_fullscreen_connected`` ensures we only connect the
        # image viewer's full screen exit signal once per controller lifetime.
        self._edit_viewer_fullscreen_connected = False
        # Track whether the dedicated edit full screen workflow is active so
        # other components (for example the frameless window manager or the
        # shortcut handler) can delegate exit requests appropriately.
        self._fullscreen_active = False
        # ``_fullscreen_hidden_widgets`` stores the visibility state of chrome
        # elements that should disappear while the dedicated edit full screen is
        # active.  Restoring the recorded state ensures we respect whichever
        # widgets were already hidden before the transition.
        self._fullscreen_hidden_widgets: list[tuple[QWidget, bool]] = []
        # Persist the splitter geometry before collapsing the navigation and
        # edit sidebars so we can restore the layout verbatim when the user
        # exits full screen preview mode.
        self._fullscreen_splitter_sizes: list[int] | None = None
        # Record the edit sidebar's width constraints before they are relaxed
        # for full screen mode.  The user may have resized the pane manually, so
        # reinstating the saved bounds keeps their layout intact after exit.
        self._fullscreen_edit_sidebar_constraints: tuple[int, int] | None = None
        ui.edit_reset_button.clicked.connect(self._handle_reset_clicked)
        ui.edit_done_button.clicked.connect(self._handle_done_clicked)
        ui.edit_adjust_action.triggered.connect(lambda checked: self._handle_mode_change("adjust", checked))
        ui.edit_crop_action.triggered.connect(lambda checked: self._handle_mode_change("crop", checked))
        ui.edit_compare_button.pressed.connect(self._handle_compare_pressed)
        ui.edit_compare_button.released.connect(self._handle_compare_released)
        ui.edit_mode_control.currentIndexChanged.connect(self._handle_top_bar_index_changed)

        playlist.currentChanged.connect(self._handle_playlist_change)
        playlist.sourceChanged.connect(lambda _path: self._handle_playlist_change())

        ui.edit_header_container.hide()

    # ------------------------------------------------------------------
    # Zoom toolbar management
    # ------------------------------------------------------------------
    def _connect_edit_zoom_controls(self) -> None:
        """Connect the shared zoom toolbar to the edit image viewer."""

        if self._edit_zoom_controls_connected:
            return

        viewer = self._ui.edit_image_viewer
        self._ui.zoom_in_button.clicked.connect(viewer.zoom_in)
        self._ui.zoom_out_button.clicked.connect(viewer.zoom_out)
        self._ui.zoom_slider.valueChanged.connect(self._handle_edit_zoom_slider_changed)
        viewer.zoomChanged.connect(self._handle_edit_viewer_zoom_changed)
        self._edit_zoom_controls_connected = True

    def _disconnect_edit_zoom_controls(self) -> None:
        """Detach the shared zoom toolbar from the edit image viewer."""

        if not self._edit_zoom_controls_connected:
            return

        viewer = self._ui.edit_image_viewer
        try:
            self._ui.zoom_in_button.clicked.disconnect(viewer.zoom_in)
            self._ui.zoom_out_button.clicked.disconnect(viewer.zoom_out)
            self._ui.zoom_slider.valueChanged.disconnect(self._handle_edit_zoom_slider_changed)
            viewer.zoomChanged.disconnect(self._handle_edit_viewer_zoom_changed)
        finally:
            # Ensure the state flag is cleared even if Qt reports that some of the links had already
            # been severed.  The warning-prone duplicate disconnect attempts should now be guarded by
            # the boolean check above, but resetting the flag keeps the controller resilient in case
            # future refactors bypass the helper inadvertently.
            self._edit_zoom_controls_connected = False

    def _handle_edit_zoom_slider_changed(self, value: int) -> None:
        """Translate slider *value* percentages into edit viewer zoom factors."""

        slider = self._ui.zoom_slider
        clamped = max(slider.minimum(), min(slider.maximum(), value))
        factor = float(clamped) / 100.0
        viewer = self._ui.edit_image_viewer
        viewer.set_zoom(factor, anchor=viewer.viewport_center())

    def _handle_edit_viewer_zoom_changed(self, factor: float) -> None:
        """Synchronise the slider position when the edit viewer reports a new zoom *factor*."""

        slider = self._ui.zoom_slider
        slider_value = max(slider.minimum(), min(slider.maximum(), int(round(factor * 100.0))))
        if slider_value == slider.value():
            return
        slider.blockSignals(True)
        slider.setValue(slider_value)
        slider.blockSignals(False)

    # ------------------------------------------------------------------
    def begin_edit(self) -> None:
        """Enter the edit view for the playlist's current asset."""

        if self._view_controller.is_edit_view_active():
            return
        source = self._playlist.current_source()
        if source is None:
            return
        image = image_loader.load_qimage(source)
        if image is None or image.isNull():
            return

        self._current_source = source

        adjustments = sidecar.load_adjustments(source)

        session = EditSession(self)
        session.set_values(adjustments, emit_individual=False)
        session.valuesChanged.connect(self._handle_session_changed)
        self._session = session

        self._ui.edit_sidebar.set_session(session)
        self._ui.edit_sidebar.refresh()
        if not self._edit_viewer_fullscreen_connected:
            # Route double-click exit requests from the edit viewer through the
            # controller so we can restore the chrome even when immersive mode
            # is managed outside the frameless window manager.
            self._ui.edit_image_viewer.fullscreenExitRequested.connect(self.exit_fullscreen_preview)
            self._edit_viewer_fullscreen_connected = True

        initial_pixmap = self._preview_manager.start_session(image, session.values())
        self._compare_active = False
        if initial_pixmap.isNull():
            self._ui.edit_image_viewer.clear()
        else:
            self._ui.edit_image_viewer.set_pixmap(initial_pixmap)
        self._set_mode("adjust")

        self._move_header_widgets_for_edit()
        if self._detail_ui_controller is not None:
            self._detail_ui_controller.disconnect_zoom_controls()
        self._connect_edit_zoom_controls()
        self._ui.edit_image_viewer.reset_zoom()
        self._view_controller.show_edit_view()
        self._transition_manager.enter_edit_mode(animate=True)

        self.editingStarted.emit(source)

    def leave_edit_mode(self, animate: bool = True) -> None:
        """Return to the standard detail view, optionally animating the transition."""

        self._preview_manager.cancel_pending_updates()
        if self._fullscreen_active:
            self.exit_fullscreen_preview()
        if (
            not self._view_controller.is_edit_view_active()
            and not self._transition_manager.is_transition_active()
        ):
            return

        # Ensure the preview surface shows the latest adjusted frame before any widgets start
        # disappearing so the user never sees a partially restored original.
        self._handle_compare_released()

        self._disconnect_edit_zoom_controls()
        if self._detail_ui_controller is not None:
            self._detail_ui_controller.connect_zoom_controls()
        self._restore_header_widgets_after_edit()
        if self._edit_viewer_fullscreen_connected:
            try:
                self._ui.edit_image_viewer.fullscreenExitRequested.disconnect(self.exit_fullscreen_preview)
            except (TypeError, RuntimeError):
                pass
            self._edit_viewer_fullscreen_connected = False
        self._view_controller.show_detail_view()

        self._transition_manager.leave_edit_mode(animate=animate)
        self._preview_manager.stop_session()

    # ------------------------------------------------------------------
    def _handle_session_changed(self, values: dict) -> None:
        del values  # The session retains the authoritative mapping internally.
        if self._session is None:
            return
        self._preview_manager.update_adjustments(self._session.values())

    def _on_preview_pixmap_updated(self, pixmap: QPixmap) -> None:
        """Display *pixmap* unless the compare gesture is active."""

        if self._compare_active:
            return
        if pixmap.isNull():
            self._ui.edit_image_viewer.clear()
            return
        self._ui.edit_image_viewer.set_pixmap(pixmap)

    def _on_transition_finished(self, direction: str) -> None:
        """Clean up controller state after the transition manager completes."""

        if direction == "exit":
            self._ui.edit_header_container.hide()
            self._ui.edit_sidebar.set_session(None)
            self._ui.edit_image_viewer.clear()
            self._session = None
            self._current_source = None
            self._compare_active = False

    # ------------------------------------------------------------------
    # Dedicated edit full screen workflow
    # ------------------------------------------------------------------
    def is_in_fullscreen(self) -> bool:
        """Return ``True`` when the dedicated edit full screen mode is active."""

        return self._fullscreen_active

    def enter_fullscreen_preview(self) -> None:
        """Expand the edit viewer into a chrome-free full screen mode."""

        if self._fullscreen_active:
            return
        if not self._view_controller.is_edit_view_active():
            return
        if self._current_source is None or self._session is None:
            return
        if not isinstance(self._window, QWidget):
            return

        full_res_image = image_loader.load_qimage(self._current_source)
        if full_res_image is None or full_res_image.isNull():
            _LOGGER.warning(
                "Failed to load full resolution image for %s", self._current_source
            )
            return

        try:
            initial_pixmap = self._preview_manager.start_session(
                full_res_image,
                self._session.values(),
                scale_for_viewport=False,
            )
        except Exception:
            _LOGGER.warning(
                "Failed to initialise full screen preview session for %s",
                self._current_source,
            )
            return

        # Hide the standard chrome so the image occupies the entire window.
        edit_sidebar = self._ui.edit_sidebar
        self._fullscreen_edit_sidebar_constraints = (
            edit_sidebar.minimumWidth(),
            edit_sidebar.maximumWidth(),
        )
        widgets_to_hide = [
            self._ui.window_chrome,
            self._ui.sidebar,
            self._ui.status_bar,
            self._ui.edit_header_container,
            edit_sidebar,
        ]
        self._fullscreen_hidden_widgets = []
        for widget in widgets_to_hide:
            self._fullscreen_hidden_widgets.append((widget, widget.isVisible()))
            widget.hide()

        # Collapse both sidebars so the edit viewer can stretch across the
        # entire screen without splitter-imposed constraints.
        edit_sidebar.setMinimumWidth(0)
        edit_sidebar.setMaximumWidth(0)
        edit_sidebar.updateGeometry()
        navigation_sidebar = self._ui.sidebar
        relax_navigation = getattr(navigation_sidebar, "relax_minimum_width_for_animation", None)
        if callable(relax_navigation):
            relax_navigation()
        splitter = self._ui.splitter
        self._fullscreen_splitter_sizes = self._sanitise_splitter_sizes(splitter.sizes())
        total = sum(self._fullscreen_splitter_sizes or [])
        if total <= 0:
            total = max(1, splitter.width())
        splitter.setSizes([0, total])

        self._window.showFullScreen()

        self._fullscreen_active = True
        self._compare_active = False

        if initial_pixmap.isNull():
            self._ui.edit_image_viewer.clear()
        else:
            self._ui.edit_image_viewer.set_pixmap(initial_pixmap)
        self._ui.edit_image_viewer.reset_zoom()

    def exit_fullscreen_preview(self) -> None:
        """Restore the standard edit chrome after leaving full screen."""

        if not self._fullscreen_active:
            return
        if not isinstance(self._window, QWidget):
            return

        self._preview_manager.cancel_pending_updates()
        self._window.showNormal()

        # Reinstate chrome elements using the visibility state captured on
        # entry so we respect any widgets the caller had already hidden.
        for widget, was_visible in self._fullscreen_hidden_widgets:
            widget.setVisible(was_visible)
        self._fullscreen_hidden_widgets = []

        navigation_sidebar = self._ui.sidebar
        restore_navigation = getattr(
            navigation_sidebar,
            "restore_minimum_width_after_animation",
            None,
        )
        if callable(restore_navigation):
            restore_navigation()

        if self._fullscreen_edit_sidebar_constraints is not None:
            min_width, max_width = self._fullscreen_edit_sidebar_constraints
            edit_sidebar = self._ui.edit_sidebar
            edit_sidebar.setMinimumWidth(min_width)
            edit_sidebar.setMaximumWidth(max_width)
            edit_sidebar.updateGeometry()
        self._fullscreen_edit_sidebar_constraints = None

        if self._fullscreen_splitter_sizes:
            self._ui.splitter.setSizes(self._fullscreen_splitter_sizes)
        self._fullscreen_splitter_sizes = None

        self._fullscreen_active = False

        if self._current_source is None or self._session is None:
            self._preview_manager.stop_session()
            return

        source_image = self._preview_manager.get_base_image()
        if source_image is None or source_image.isNull():
            source_image = image_loader.load_qimage(self._current_source)
        if source_image is None or source_image.isNull():
            self._preview_manager.stop_session()
            self._ui.edit_image_viewer.clear()
            return

        try:
            initial_pixmap = self._preview_manager.start_session(
                source_image,
                self._session.values(),
                scale_for_viewport=True,
            )
        except Exception:
            _LOGGER.warning(
                "Failed to restore standard preview session for %s",
                self._current_source,
            )
            self._preview_manager.stop_session()
            return

        self._compare_active = False
        if initial_pixmap.isNull():
            self._ui.edit_image_viewer.clear()
        else:
            self._ui.edit_image_viewer.set_pixmap(initial_pixmap)
        self._ui.edit_image_viewer.reset_zoom()

    def _handle_compare_pressed(self) -> None:
        """Display the original photo while the compare button is held."""

        base_pixmap = self._preview_manager.get_base_image_pixmap()
        if base_pixmap is None or base_pixmap.isNull():
            return
        self._compare_active = True
        self._ui.edit_image_viewer.set_pixmap(base_pixmap)

    def _handle_compare_released(self) -> None:
        """Restore the adjusted preview after a comparison glance."""

        self._compare_active = False
        preview_pixmap = self._preview_manager.get_current_preview_pixmap()
        if preview_pixmap is not None and not preview_pixmap.isNull():
            self._ui.edit_image_viewer.set_pixmap(preview_pixmap)
            return
        base_pixmap = self._preview_manager.get_base_image_pixmap()
        if base_pixmap is not None and not base_pixmap.isNull():
            # Fall back to the unadjusted preview if a recalculated frame is not available.
            self._ui.edit_image_viewer.set_pixmap(base_pixmap)

    def _handle_reset_clicked(self) -> None:
        if self._session is None:
            return
        # Stop any pending preview updates so the reset renders immediately.
        self._preview_manager.cancel_pending_updates()
        self._session.reset()

    def _handle_done_clicked(self) -> None:
        # Ensure no delayed preview runs after committing the adjustments.
        self._preview_manager.stop_session()
        if self._session is None or self._current_source is None:
            self.leave_edit_mode(animate=True)
            return
        # Store the source path locally before ``leave_edit_mode`` clears the
        # controller state.  The detail player needs the same asset path to
        # reload the freshly saved adjustments once the edit chrome is hidden.
        source = self._current_source
        # Capture the rendered preview so the detail view can reuse it while
        # the background worker recalculates the full-resolution frame.  A copy
        # keeps the pixmap alive after the edit widgets tear down their state.
        preview_pixmap = self._ui.edit_image_viewer.pixmap()
        adjustments = self._session.values()
        if self._navigation is not None:
            # Saving adjustments writes sidecar files, which triggers the
            # filesystem watcher to rebuild the sidebar tree.  That rebuild
            # reselects the active collection ("All Photos", etc.) and would
            # otherwise emit navigation signals that yank the UI back to the
            # gallery.  Arm the suppression guard *before* touching the disk so
            # those callbacks are ignored until the detail surface finishes
            # updating.
            self._navigation.suppress_tree_refresh_for_edit()
        sidecar.save_adjustments(source, adjustments)
        # Defer cache invalidation until after the detail surface has been
        # restored so the gallery does not briefly become the active view while
        # its thumbnails refresh in the background.
        self._schedule_thumbnail_refresh(source)
        self.leave_edit_mode(animate=True)
        # ``display_image`` schedules an asynchronous reload; logging the
        # boolean result would not improve the UX, so simply trigger it and
        # fall back to the playlist selection handlers if scheduling fails.
        self._player_view.display_image(source, placeholder=preview_pixmap)
        self.editingFinished.emit(source)

    def _refresh_thumbnail_cache(self, source: Path) -> None:
        metadata = self._asset_model.source_model().metadata_for_absolute_path(source)
        if metadata is None:
            return
        rel_value = metadata.get("rel")
        if not rel_value:
            return
        self._refresh_thumbnail_cache_for_rel(str(rel_value))

    def _refresh_thumbnail_cache_for_rel(self, rel: str) -> None:
        """Invalidate cached thumbnails identified by *rel*."""

        if not rel:
            return
        source_model = self._asset_model.source_model()
        if hasattr(source_model, "invalidate_thumbnail"):
            source_model.invalidate_thumbnail(rel)
        self._thumbnail_loader.invalidate(rel)

    def _schedule_thumbnail_refresh(self, source: Path) -> None:
        """Refresh thumbnails for *source* on the next event loop turn.

        The deferment avoids jarring view changes that occur when the gallery
        reacts to cache invalidation while the user is still focused on the
        detail surface.
        """

        metadata = self._asset_model.source_model().metadata_for_absolute_path(source)
        if metadata is None:
            return
        rel_value = metadata.get("rel")
        if not rel_value:
            return
        rel = str(rel_value)
        if rel in self._pending_thumbnail_refreshes:
            return

        def _run_refresh(rel_key: str) -> None:
            try:
                self._refresh_thumbnail_cache_for_rel(rel_key)
            finally:
                self._pending_thumbnail_refreshes.discard(rel_key)

        self._pending_thumbnail_refreshes.add(rel)
        QTimer.singleShot(0, lambda rel_key=rel: _run_refresh(rel_key))

    def _handle_mode_change(self, mode: str, checked: bool) -> None:
        if not checked:
            return
        self._set_mode(mode)

    def _handle_top_bar_index_changed(self, index: int) -> None:
        """Synchronise action state when the segmented bar changes selection."""

        mode = "adjust" if index == 0 else "crop"
        target_action = self._ui.edit_adjust_action if mode == "adjust" else self._ui.edit_crop_action
        if not target_action.isChecked():
            target_action.setChecked(True)
        self._set_mode(mode, from_top_bar=True)

    def _set_mode(self, mode: str, *, from_top_bar: bool = False) -> None:
        if mode == "adjust":
            self._ui.edit_adjust_action.setChecked(True)
            self._ui.edit_crop_action.setChecked(False)
            self._ui.edit_sidebar.set_mode("adjust")
        else:
            self._ui.edit_adjust_action.setChecked(False)
            self._ui.edit_crop_action.setChecked(True)
            self._ui.edit_sidebar.set_mode("crop")
        index = 0 if mode == "adjust" else 1
        self._ui.edit_mode_control.setCurrentIndex(index, animate=not from_top_bar)

    def _move_header_widgets_for_edit(self) -> None:
        """Reparent shared toolbar widgets into the edit header."""

        ui = self._ui
        if ui.edit_zoom_host_layout.indexOf(ui.zoom_widget) == -1:
            ui.edit_zoom_host_layout.addWidget(ui.zoom_widget)
        ui.zoom_widget.show()

        right_layout = ui.edit_right_controls_layout
        if right_layout.indexOf(ui.info_button) == -1:
            right_layout.insertWidget(0, ui.info_button)
        if right_layout.indexOf(ui.favorite_button) == -1:
            right_layout.insertWidget(1, ui.favorite_button)

    def _restore_header_widgets_after_edit(self) -> None:
        """Return shared toolbar widgets to the detail header layout."""

        ui = self._ui
        ui.detail_actions_layout.insertWidget(ui.detail_info_button_index, ui.info_button)
        ui.detail_actions_layout.insertWidget(ui.detail_favorite_button_index, ui.favorite_button)
        ui.detail_header_layout.insertWidget(ui.detail_zoom_widget_index, ui.zoom_widget)

    def _handle_playlist_change(self) -> None:
        if self._view_controller.is_edit_view_active():
            self.leave_edit_mode()
    def set_navigation_controller(self, navigation: "NavigationController") -> None:
        """Attach the navigation controller after construction.

        The main window builds the view controllers before wiring the
        navigation stack.  Providing a setter keeps the constructor flexible
        while still allowing the edit workflow to coordinate suppression of
        sidebar-driven navigation callbacks when adjustments are saved.
        """

        self._navigation = navigation

