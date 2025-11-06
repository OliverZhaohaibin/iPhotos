"""Controller coordinating the edit view and non-destructive adjustments."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal
from PySide6.QtGui import QImage

from ..models.asset_model import AssetModel
from ..models.edit_session import EditSession
from ..tasks.adjustment_workers import (
    AdjustmentLoadWorker,
    AdjustmentSaveWorker,
)
from ..tasks.image_load_worker import ImageLoadWorker
from ..tasks.thumbnail_loader import ThumbnailLoader
from ..ui_main_window import Ui_MainWindow
from .edit_fullscreen_manager import EditFullscreenManager
from .edit_preview_manager import EditPreviewManager, resolve_adjustment_mapping
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

        # Track the background image loader so we can avoid queueing multiple
        # decode jobs for the same asset if the user re-enters the edit view
        # quickly.  The worker owns the heavy file I/O, leaving the GUI thread
        # free to animate the transition into the edit chrome.
        self._active_image_worker: ImageLoadWorker | None = None
        self._is_loading_edit_image = False

        self._preview_manager = EditPreviewManager(self._ui.edit_image_viewer, self)
        self._preview_manager.color_stats_ready.connect(
            lambda _stats: self._apply_session_adjustments_to_viewer()
        )

        self._transition_manager = EditViewTransitionManager(self._ui, self._window, self)
        self._transition_manager.transition_finished.connect(self._on_transition_finished)
        self._transition_manager.set_detail_ui_controller(self._detail_ui_controller)

        self._fullscreen_manager = EditFullscreenManager(
            self._ui,
            self._window,
            self._preview_manager,
            self,
        )

        self._session: Optional[EditSession] = None
        self._current_source: Optional[Path] = None
        self._compare_active = False
        # ``_active_adjustments`` caches the resolved shader values representing the
        # most recent session state.  Storing the mapping lets compare mode and
        # the full screen workflow reapply the exact GPU uniforms without
        # recalculating them repeatedly.
        self._active_adjustments: dict[str, float] = {}
        self._skip_next_preview_frame = False
        # ``_edit_viewer_fullscreen_connected`` ensures we only connect the
        # image viewer's full screen exit signal once per controller lifetime.
        self._edit_viewer_fullscreen_connected = False
        self._active_adjustment_worker: AdjustmentLoadWorker | None = None
        self._adjustment_load_job_id = 0
        self._active_save_workers: set[AdjustmentSaveWorker] = set()
        ui.edit_reset_button.clicked.connect(self._handle_reset_clicked)
        ui.edit_done_button.clicked.connect(self._handle_done_clicked)
        ui.edit_adjust_action.triggered.connect(lambda checked: self._handle_mode_change("adjust", checked))
        ui.edit_crop_action.triggered.connect(lambda checked: self._handle_mode_change("crop", checked))
        ui.edit_compare_button.pressed.connect(self._handle_compare_pressed)
        ui.edit_compare_button.released.connect(self._handle_compare_released)
        ui.edit_mode_control.currentIndexChanged.connect(self._handle_top_bar_index_changed)
        ui.edit_info_button.clicked.connect(lambda: ui.info_button.click())
        ui.edit_favorite_button.clicked.connect(lambda: ui.favorite_button.click())
        ui.info_button.clicked.connect(lambda: QTimer.singleShot(0, self._sync_edit_header_buttons))
        ui.favorite_button.clicked.connect(
            lambda: QTimer.singleShot(0, self._sync_edit_header_buttons)
        )

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
        self._ui.edit_zoom_in_button.clicked.connect(viewer.zoom_in)
        self._ui.edit_zoom_out_button.clicked.connect(viewer.zoom_out)
        self._ui.edit_zoom_slider.valueChanged.connect(self._handle_edit_zoom_slider_changed)
        viewer.zoomChanged.connect(self._handle_edit_viewer_zoom_changed)
        self._edit_zoom_controls_connected = True

    def _disconnect_edit_zoom_controls(self) -> None:
        """Detach the shared zoom toolbar from the edit image viewer."""

        if not self._edit_zoom_controls_connected:
            return

        viewer = self._ui.edit_image_viewer
        try:
            self._ui.edit_zoom_in_button.clicked.disconnect(viewer.zoom_in)
            self._ui.edit_zoom_out_button.clicked.disconnect(viewer.zoom_out)
            self._ui.edit_zoom_slider.valueChanged.disconnect(self._handle_edit_zoom_slider_changed)
            viewer.zoomChanged.disconnect(self._handle_edit_viewer_zoom_changed)
        finally:
            # Ensure the state flag is cleared even if Qt reports that some of the links had already
            # been severed.  The warning-prone duplicate disconnect attempts should now be guarded by
            # the boolean check above, but resetting the flag keeps the controller resilient in case
            # future refactors bypass the helper inadvertently.
            self._edit_zoom_controls_connected = False

    def _handle_edit_zoom_slider_changed(self, value: int) -> None:
        """Translate slider *value* percentages into edit viewer zoom factors."""

        slider = self._ui.edit_zoom_slider
        clamped = max(slider.minimum(), min(slider.maximum(), value))
        factor = float(clamped) / 100.0
        viewer = self._ui.edit_image_viewer
        viewer.set_zoom(factor, anchor=viewer.viewport_center())

    def _handle_edit_viewer_zoom_changed(self, factor: float) -> None:
        """Synchronise the slider position when the edit viewer reports a new zoom *factor*."""

        slider = self._ui.edit_zoom_slider
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
        self._current_source = source

        session = EditSession(self)
        session.valuesChanged.connect(self._handle_session_changed)
        self._session = session
        self._apply_session_adjustments_to_viewer()

        # Detect whether the detail view already uploaded the same image so the
        # edit transition can reuse the existing GPU texture without a
        # redundant re-upload.  When the source matches we keep the user's
        # zoom/pan framing intact.
        viewer = self._ui.edit_image_viewer
        current_source = viewer.current_image_source()
        self._skip_next_preview_frame = current_source == source
        if not self._skip_next_preview_frame:
            viewer.reset_zoom()

        # Clear any stale preview content before attaching the fresh session.  The sidebar reuses
        # its last preview image until it receives an explicit replacement, so resetting it ahead
        # of the new binding avoids showing thumbnails from the previously edited asset while the
        # replacement image is loading in the background.
        self._ui.edit_sidebar.set_light_preview_image(None)
        self._ui.edit_sidebar.set_session(session)
        self._ui.edit_sidebar.refresh()
        if not self._edit_viewer_fullscreen_connected:
            # Route double-click exit requests from the edit viewer through the
            # controller so the dedicated full screen manager can restore the
            # chrome even when immersive mode is triggered outside the
            # frameless window manager.
            self._ui.edit_image_viewer.fullscreenExitRequested.connect(
                self.exit_fullscreen_preview
            )
            self._edit_viewer_fullscreen_connected = True

        self._compare_active = False
        self._set_mode("adjust")

        self._move_header_widgets_for_edit()
        if self._detail_ui_controller is not None:
            self._detail_ui_controller.disconnect_zoom_controls()
        self._connect_edit_zoom_controls()
        self._view_controller.show_edit_view()
        self._transition_manager.enter_edit_mode(animate=True)

        self.editingStarted.emit(source)

        # Start loading the full resolution image on the worker pool immediately so the
        # transition animation can play without waiting for disk I/O or decode work.
        self._start_async_edit_load(source)
        self._start_async_adjustment_load(source)

    def _start_async_edit_load(self, source: Path) -> None:
        """Kick off the threaded image load for *source*."""

        if self._session is None:
            return
        # Reset any previous worker reference so a stale ``ImageLoadWorker`` finishing late does
        # not try to update widgets for an unrelated asset.  The session check above already guards
        # against most late deliveries, but clearing the pointer avoids keeping unnecessary objects
        # alive.
        self._active_image_worker = None
        self._is_loading_edit_image = True
        self._ui.edit_image_viewer.set_loading(True)

        worker = ImageLoadWorker(source)
        worker.signals.imageLoaded.connect(self._on_edit_image_loaded)
        worker.signals.loadFailed.connect(self._on_edit_image_load_failed)
        self._active_image_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _start_async_adjustment_load(self, source: Path) -> None:
        """Load persisted adjustments without blocking the GUI thread."""

        self._adjustment_load_job_id += 1
        job_id = self._adjustment_load_job_id
        worker = AdjustmentLoadWorker(source)
        self._active_adjustment_worker = worker

        def _handle_loaded(path: Path, adjustments: dict, *, expected_job=job_id) -> None:
            if expected_job != self._adjustment_load_job_id:
                return
            self._active_adjustment_worker = None
            self._on_adjustments_loaded(path, adjustments)

        def _handle_failed(path: Path, message: str, *, expected_job=job_id) -> None:
            if expected_job != self._adjustment_load_job_id:
                return
            self._active_adjustment_worker = None
            self._on_adjustments_load_failed(path, message)

        worker.signals.loaded.connect(_handle_loaded)
        worker.signals.failed.connect(_handle_failed)
        QThreadPool.globalInstance().start(worker)

    def _on_adjustments_loaded(self, path: Path, adjustments: dict) -> None:
        """Apply *adjustments* delivered by the background load."""

        if self._session is None or self._current_source != path:
            return
        if adjustments:
            self._session.set_values(adjustments, emit_individual=False)

    def _on_adjustments_load_failed(self, path: Path, message: str) -> None:
        """Log a failure to load adjustments for *path* without aborting edit mode."""

        del path  # The controller already records the active source separately.
        _LOGGER.warning("Failed to load adjustments for edit session: %s", message)

    def _start_async_adjustment_save(self, source: Path, adjustments: dict[str, float | bool]) -> None:
        """Persist *adjustments* to disk on a worker thread."""

        worker = AdjustmentSaveWorker(source, adjustments)
        self._active_save_workers.add(worker)

        def _handle_success(path: Path, *, worker_ref=worker) -> None:
            self._active_save_workers.discard(worker_ref)
            self._on_adjustments_saved(path)

        def _handle_failure(path: Path, message: str, *, worker_ref=worker) -> None:
            self._active_save_workers.discard(worker_ref)
            self._on_adjustments_save_failed(path, message)

        worker.signals.succeeded.connect(_handle_success)
        worker.signals.failed.connect(_handle_failure)
        try:
            QThreadPool.globalInstance().start(worker)
        except RuntimeError as exc:
            self._active_save_workers.discard(worker)
            self._on_adjustments_save_failed(source, str(exc))

    def _on_adjustments_saved(self, path: Path) -> None:
        """Refresh thumbnails after the background save finishes."""

        self._schedule_thumbnail_refresh(path)
        if self._navigation is not None:
            try:
                self._navigation.release_tree_refresh_suppression_if_edit()
            except AttributeError:
                pass

    def _on_adjustments_save_failed(self, path: Path, message: str) -> None:
        """Report sidecar save failures while releasing navigation suppression."""

        _LOGGER.error("Failed to save adjustments for %s: %s", path, message)
        if self._navigation is not None:
            try:
                self._navigation.release_tree_refresh_suppression_if_edit()
            except AttributeError:
                pass

    def _on_edit_image_loaded(self, path: Path, image: QImage) -> None:
        """Handle a successfully decoded edit image."""

        self._active_image_worker = None
        if self._session is None or self._current_source != path:
            return
        if not self._view_controller.is_edit_view_active():
            return

        session = self._session

        if not self._edit_viewer_fullscreen_connected:
            self._ui.edit_image_viewer.fullscreenExitRequested.connect(
                self.exit_fullscreen_preview
            )
            self._edit_viewer_fullscreen_connected = True

        try:
            self._preview_manager.start_session(image, session.values())
        except Exception:
            _LOGGER.exception("Failed to initialise preview session for %s", path)
            self._is_loading_edit_image = False
            self._ui.edit_image_viewer.set_loading(False)
            self.leave_edit_mode(animate=False)
            return

        if self._skip_next_preview_frame:
            # The detail surface already uploaded the same GPU texture.  Keep the existing texture
            # alive and only refresh the shader uniforms so the transition feels instantaneous.
            self._skip_next_preview_frame = False
            self._apply_session_adjustments_to_viewer()
        else:
            # A brand new asset is entering edit mode; upload its pixels once and preserve the zoom
            # transform so the edit UI does not snap back to a centred view mid-transition.
            resolved_adjustments = self._resolve_session_adjustments()
            self._active_adjustments = resolved_adjustments
            self._ui.edit_image_viewer.set_image(
                image,
                resolved_adjustments,
                image_source=path,
                reset_view=False,
            )
            if self._compare_active:
                # Honour an active compare press by restoring the original look immediately.
                self._ui.edit_image_viewer.set_adjustments({})

        # The worker has delivered the full-resolution frame, so the loading scrim can disappear
        # immediately.  This mirrors the legacy behaviour where the QWidget-based viewer stopped
        # animating the spinner as soon as decoding finished.
        self._is_loading_edit_image = False
        self._ui.edit_image_viewer.set_loading(False)

        # Hand the decoded frame to the sidebar so the thumbnail workers can begin rendering their
        # filtered previews without blocking the GUI thread.
        self._ui.edit_sidebar.set_light_preview_image(image)
        self._ui.edit_sidebar.refresh()

    def _on_edit_image_load_failed(self, path: Path, message: str) -> None:
        """Abort the edit flow when the source image fails to load."""

        del path  # The controller already stores the active source path separately.
        self._active_image_worker = None
        if not self._is_loading_edit_image:
            return
        self._is_loading_edit_image = False
        self._ui.edit_image_viewer.set_loading(False)
        self._skip_next_preview_frame = False
        _LOGGER.error("Failed to load image for editing: %s", message)
        self.leave_edit_mode(animate=False)

    def leave_edit_mode(self, animate: bool = True) -> None:
        """Return to the standard detail view, optionally animating the transition."""

        self._preview_manager.cancel_pending_updates()
        if self._is_loading_edit_image:
            self._is_loading_edit_image = False
            self._ui.edit_image_viewer.set_loading(False)
        if self._fullscreen_manager.is_in_fullscreen():
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
                self._ui.edit_image_viewer.fullscreenExitRequested.disconnect(
                    self.exit_fullscreen_preview
                )
            except (TypeError, RuntimeError):
                pass
            self._edit_viewer_fullscreen_connected = False
        self._view_controller.show_detail_view()

        self._transition_manager.leave_edit_mode(animate=animate)
        self._preview_manager.stop_session()
        self._skip_next_preview_frame = False

    # ------------------------------------------------------------------
    def _handle_session_changed(self, values: dict) -> None:
        del values  # The session retains the authoritative mapping internally.
        if self._session is None:
            return
        self._preview_manager.update_adjustments(self._session.values())
        self._apply_session_adjustments_to_viewer()

    def _apply_session_adjustments_to_viewer(self) -> None:
        """Forward the latest session values to the GL viewer."""

        if self._session is None:
            return
        adjustments = self._resolve_session_adjustments()
        self._active_adjustments = adjustments
        # Avoid repainting while the compare button is held so the user continues
        # to see the unadjusted frame until the interaction ends.
        if not self._compare_active:
            self._ui.edit_image_viewer.set_adjustments(adjustments)

    def _resolve_session_adjustments(self) -> dict[str, float]:
        """Return the current session adjustments resolved for the GL shader."""

        if self._session is None:
            return {}

        values = self._session.values()

        # Prefer the preview manager helper so both the CPU thumbnails and the GPU shader share
        # identical Photos-compatible math.  Fall back to the module-level resolver if the preview
        # pipeline has not been initialised yet (for example, during early unit tests).
        try:
            resolved = self._preview_manager.resolve_adjustments(values)
        except AttributeError:
            resolved = resolve_adjustment_mapping(values, stats=self._preview_manager.color_stats())
        else:
            # ``resolve_adjustments`` already honours colour statistics when available.  Guard the
            # return value here so callers always receive a defensive copy and accidental mutation
            # cannot leak back into the preview manager's caches.
            resolved = dict(resolved)

        return resolved

    def _restore_active_adjustments(self) -> None:
        """Reapply the cached adjustments to the GL viewer."""

        if self._compare_active:
            return
        if self._active_adjustments:
            self._ui.edit_image_viewer.set_adjustments(self._active_adjustments)
        else:
            self._ui.edit_image_viewer.set_adjustments({})

    def _on_transition_finished(self, direction: str) -> None:
        """Clean up controller state after the transition manager completes."""

        if direction == "exit":
            self._ui.edit_header_container.hide()
            self._ui.edit_sidebar.set_session(None)
            self._ui.edit_sidebar.set_light_preview_image(None)
            # Do not call ``clear`` on the shared GL viewer here.  The retained texture lets the
            # detail surface display the adjusted frame instantly when the edit chrome slides away.
            self._session = None
            self._current_source = None
            self._compare_active = False
            self._active_adjustments = {}
            self._skip_next_preview_frame = False

    # ------------------------------------------------------------------
    # Dedicated edit full screen workflow
    # ------------------------------------------------------------------
    def is_in_fullscreen(self) -> bool:
        """Expose the immersive full screen state managed externally."""

        return self._fullscreen_manager.is_in_fullscreen()

    def enter_fullscreen_preview(self) -> None:
        """Expand the edit viewer into a chrome-free full screen mode."""

        if not self._view_controller.is_edit_view_active():
            return
        if self._current_source is None or self._session is None:
            return

        adjustments = self._active_adjustments or self._resolve_session_adjustments()
        if self._fullscreen_manager.enter_fullscreen_preview(
            self._current_source,
            adjustments,
        ):
            self._compare_active = False

    def exit_fullscreen_preview(self) -> None:
        """Restore the standard edit chrome after leaving full screen."""

        adjustments: Optional[dict[str, float]] = None
        if self._session is not None:
            adjustments = self._active_adjustments or self._resolve_session_adjustments()

        if self._fullscreen_manager.exit_fullscreen_preview(
            self._current_source,
            adjustments,
        ):
            self._compare_active = False

    def _handle_compare_pressed(self) -> None:
        """Display the original photo while the compare button is held."""

        self._compare_active = True
        self._ui.edit_image_viewer.set_adjustments({})

    def _handle_compare_released(self) -> None:
        """Restore the adjusted preview after a comparison glance."""

        self._compare_active = False
        self._restore_active_adjustments()

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
        adjustments = self._session.values()
        resolved_adjustments = self._active_adjustments or self._resolve_session_adjustments()
        if self._navigation is not None:
            # Saving adjustments writes sidecar files, which triggers the
            # filesystem watcher to rebuild the sidebar tree.  That rebuild
            # reselects the active collection ("All Photos", etc.) and would
            # otherwise emit navigation signals that yank the UI back to the
            # gallery.  Arm the suppression guard *before* touching the disk so
            # those callbacks are ignored until the detail surface finishes
            # updating.
            self._navigation.suppress_tree_refresh_for_edit()
        self._start_async_adjustment_save(source, adjustments)
        self.leave_edit_mode(animate=True)
        # ``display_image`` schedules an asynchronous reload while immediately
        # applying the in-memory adjustments, keeping the detail view in sync
        # without forcing a GPU read-back of the edit preview surface.
        self._player_view.display_image(source, immediate_adjustments=resolved_adjustments)
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
        """Show the edit-specific controls while hiding the detail toolbar."""

        ui = self._ui
        ui.zoom_widget.hide()
        ui.info_button.hide()
        ui.favorite_button.hide()
        ui.edit_zoom_widget.show()
        ui.edit_info_button.show()
        ui.edit_favorite_button.show()
        self._sync_edit_header_buttons()

    def _restore_header_widgets_after_edit(self) -> None:
        """Restore the detail toolbar visibility after edit mode ends."""

        ui = self._ui
        ui.zoom_widget.show()
        ui.info_button.show()
        ui.favorite_button.show()
        ui.edit_zoom_widget.hide()
        ui.edit_info_button.hide()
        ui.edit_favorite_button.hide()

    def _sync_edit_header_buttons(self) -> None:
        """Mirror the detail header button state onto the edit toolbar."""

        ui = self._ui
        ui.edit_info_button.setIcon(ui.info_button.icon())
        ui.edit_info_button.setEnabled(ui.info_button.isEnabled())
        ui.edit_info_button.setToolTip(ui.info_button.toolTip())
        ui.edit_favorite_button.setIcon(ui.favorite_button.icon())
        ui.edit_favorite_button.setEnabled(ui.favorite_button.isEnabled())
        ui.edit_favorite_button.setToolTip(ui.favorite_button.toolTip())

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
