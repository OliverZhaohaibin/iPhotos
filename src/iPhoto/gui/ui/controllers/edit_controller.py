"""Controller coordinating the edit view and non-destructive adjustments."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Optional, TYPE_CHECKING

from PySide6.QtCore import (
    QObject,
    QThreadPool,
    QRunnable,
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QTimer,
    Qt,
    Property,
    Signal,
)
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect

from ....core.preview_backends import PreviewBackend, PreviewSession, select_preview_backend
from ....io import sidecar
from ...utils import image_loader
from ..models.asset_model import AssetModel
from ..models.edit_session import EditSession
from ..tasks.thumbnail_loader import ThumbnailLoader
from ..ui_main_window import Ui_MainWindow
from .player_view_controller import PlayerViewController
from .view_controller import ViewController

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from .navigation_controller import NavigationController


_LOGGER = logging.getLogger(__name__)


class _PreviewSignals(QObject):
    """Signals emitted by :class:`_PreviewWorker` once processing completes."""

    finished = Signal(QImage, int)
    """Emitted with the adjusted image and the job identifier."""


class _PreviewWorker(QRunnable):
    """Execute preview rendering in a background thread.

    The worker forwards tone-mapping requests to the selected
    :class:`~iPhoto.core.preview_backends.PreviewBackend` to keep heavy lifting
    off the UI thread.  Holding a strong reference to the backend session allows
    hardware accelerated implementations to retain any allocated resources for
    the duration of the job.
    """

    def __init__(
        self,
        backend: PreviewBackend,
        session: PreviewSession,
        adjustments: Mapping[str, float],
        job_id: int,
        signals: _PreviewSignals,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._session = session
        # Capture the adjustment mapping at the moment the job is created so the
        # session can continue to evolve without affecting in-flight work.
        self._adjustments = dict(adjustments)
        self._job_id = job_id
        self._signals = signals

    def run(self) -> None:  # type: ignore[override]
        """Perform the tone-mapping work and notify listeners when done."""

        try:
            adjusted = self._backend.render(self._session, self._adjustments)
        except Exception:
            # Propagate failures by emitting a null image.  The controller will
            # discard outdated or invalid results, so surfacing ``None`` keeps
            # the UI responsive even if processing fails unexpectedly.
            adjusted = QImage()
        self._signals.finished.emit(adjusted, self._job_id)


class _SplitterSizeAdapter(QObject):
    """Expose a Qt property that maps to :class:`QSplitter.setSizes`.

    ``QPropertyAnimation`` can only operate on QObject properties, which
    ``QSplitter`` does not provide for its child pane sizes.  The adapter wraps
    the splitter and forwards values from the animation back to
    :meth:`QSplitter.setSizes`, allowing the controller to animate the collapse
    and expansion of the navigation sidebar.
    """

    sizesChanged = Signal()
    """Signal emitted whenever the adapter writes a new size vector."""

    def __init__(self, splitter) -> None:  # type: ignore[override]
        super().__init__(splitter)
        self._splitter = splitter

    def _get_sizes(self) -> list[int]:
        """Return the current child pane sizes as a list of integers."""

        return [int(value) for value in self._splitter.sizes()]

    def _set_sizes(self, value) -> None:
        """Validate *value* and forward it to :meth:`QSplitter.setSizes`."""

        if value is None:
            return
        try:
            raw_values = list(value)
        except TypeError:
            return
        count = self._splitter.count()
        if count == 0:
            return
        # Clamp the list to the splitter's child count while preserving the total width.
        if len(raw_values) < count:
            raw_values.extend(0 for _ in range(count - len(raw_values)))
        elif len(raw_values) > count:
            raw_values = raw_values[:count]
        sanitised = [max(0, int(size)) for size in raw_values]
        total = sum(sanitised)
        if total <= 0:
            # ``QSplitter`` treats a zeroed vector as "collapse every pane".  Provide
            # a tiny non-zero width so the widget has meaningful geometry while the
            # animation initialises.
            sanitised[-1] = max(1, self._splitter.width())
        self._splitter.setSizes(sanitised)
        self.sizesChanged.emit()

    sizes = Property("QVariantList", _get_sizes, _set_sizes, notify=sizesChanged)


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
    ) -> None:
        super().__init__(parent)
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
        self._thumbnail_loader: ThumbnailLoader = asset_model.thumbnail_loader()
        # ``_pending_thumbnail_refreshes`` tracks relative asset identifiers with
        # a refresh queued via :meth:`_schedule_thumbnail_refresh`.  Deferring
        # the cache invalidation keeps the detail pane visible when edits are
        # saved, preventing the grid view from temporarily reclaiming focus just
        # so it can reload its thumbnails.
        self._pending_thumbnail_refreshes: set[str] = set()

        self._preview_backend: PreviewBackend = select_preview_backend()
        _LOGGER.info("Initialised edit preview backend: %s", self._preview_backend.tier_name)

        self._session: Optional[EditSession] = None
        self._base_image: Optional[QImage] = None
        self._current_source: Optional[Path] = None
        self._preview_session: Optional[PreviewSession] = None
        self._current_preview_pixmap: Optional[QPixmap] = None
        self._compare_active = False

        # Store geometric constraints for the animated sidebar transitions.  The UI layer
        # annotates the edit sidebar with its preferred dimensions before collapsing it to zero
        # width, so fall back gracefully if the hints are missing (for example in tests).
        preferred_width = ui.edit_sidebar.property("defaultPreferredWidth")
        minimum_width = ui.edit_sidebar.property("defaultMinimumWidth")
        maximum_width = ui.edit_sidebar.property("defaultMaximumWidth")
        self._edit_sidebar_preferred_width = max(
            1,
            int(preferred_width) if preferred_width else ui.edit_sidebar.sizeHint().width(),
        )
        self._edit_sidebar_minimum_width = max(
            1,
            int(minimum_width) if minimum_width else ui.edit_sidebar.minimumWidth(),
        )
        self._edit_sidebar_maximum_width = max(
            self._edit_sidebar_preferred_width,
            int(maximum_width) if maximum_width else ui.edit_sidebar.maximumWidth(),
        )
        self._edit_sidebar_preferred_width = min(
            self._edit_sidebar_preferred_width,
            self._edit_sidebar_maximum_width,
        )
        self._splitter_sizes_before_edit: list[int] | None = None
        self._splitter_animation_adapter = _SplitterSizeAdapter(ui.splitter)
        self._transition_group: QParallelAnimationGroup | None = None
        self._transition_direction: str | None = None

        # ``QGraphicsOpacityEffect`` allows the controller to cross-fade the edit and detail
        # headers without restructuring the layout tree.  Storing the effects avoids repeated
        # allocations and keeps the opacity state accessible to the transition helpers.
        self._edit_header_opacity = QGraphicsOpacityEffect(ui.edit_header_container)
        self._edit_header_opacity.setOpacity(1.0)
        ui.edit_header_container.setGraphicsEffect(self._edit_header_opacity)

        self._detail_header_opacity = QGraphicsOpacityEffect(ui.detail_chrome_container)
        self._detail_header_opacity.setOpacity(1.0)
        ui.detail_chrome_container.setGraphicsEffect(self._detail_header_opacity)

        # Timer used to debounce expensive preview rendering so the UI thread
        # stays responsive while the user drags a slider continuously.
        self._preview_update_timer = QTimer(self)
        self._preview_update_timer.setSingleShot(True)
        self._preview_update_timer.setInterval(50)
        self._preview_update_timer.timeout.connect(self._start_preview_job)

        # ``QThreadPool`` dispatches background preview jobs, preventing the
        # heavy pixel processing from blocking the event loop.
        self._thread_pool = QThreadPool.globalInstance()
        # Monotonic identifier used to discard stale results from superseded
        # preview jobs.
        self._preview_job_id = 0
        # Keep strong references to workers until their completion callbacks run
        # so they are not garbage collected prematurely.
        self._active_preview_workers: set[_PreviewWorker] = set()

        ui.edit_reset_button.clicked.connect(self._handle_reset_clicked)
        ui.edit_done_button.clicked.connect(self._handle_done_clicked)
        ui.edit_adjust_action.triggered.connect(lambda checked: self._handle_mode_change("adjust", checked))
        ui.edit_crop_action.triggered.connect(lambda checked: self._handle_mode_change("crop", checked))
        ui.edit_compare_button.pressed.connect(self._handle_compare_pressed)
        ui.edit_compare_button.released.connect(self._handle_compare_released)

        playlist.currentChanged.connect(self._handle_playlist_change)
        playlist.sourceChanged.connect(lambda _path: self._handle_playlist_change())

        ui.edit_header_container.hide()

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

        preview_image = self._prepare_preview_image(image)
        self._base_image = preview_image
        self._current_source = source

        # Tear down any existing backend session before creating a new one.  This
        # protects against edge-cases where ``begin_edit`` is called repeatedly
        # without leaving the view first.
        if self._preview_session is not None:
            self._preview_backend.dispose_session(self._preview_session)
        self._preview_session = self._preview_backend.create_session(preview_image)

        adjustments = sidecar.load_adjustments(source)

        session = EditSession(self)
        session.set_values(adjustments, emit_individual=False)
        session.valuesChanged.connect(self._handle_session_changed)
        self._session = session

        self._ui.edit_sidebar.set_session(session)
        self._ui.edit_sidebar.refresh()
        # Display the unadjusted preview immediately so the user sees feedback
        # while the first recalculation runs in the background.
        initial_pixmap = QPixmap.fromImage(preview_image)
        self._current_preview_pixmap = initial_pixmap
        self._compare_active = False
        self._ui.edit_image_viewer.set_pixmap(initial_pixmap)
        self._set_mode("adjust")
        self._start_preview_job()

        # Reset the header opacity so the detail chrome is fully visible the next time the
        # detail view appears.  The container is hidden immediately afterwards so the edit
        # controls can occupy the toolbar area during the edit session.
        self._detail_header_opacity.setOpacity(1.0)
        self._ui.detail_chrome_container.hide()
        self._move_header_widgets_for_edit()
        self._edit_header_opacity.setOpacity(1.0)
        self._ui.edit_header_container.show()

        splitter_sizes = self._sanitise_splitter_sizes(self._ui.splitter.sizes())
        self._splitter_sizes_before_edit = list(splitter_sizes)
        self._prepare_navigation_sidebar_for_entry()
        self._prepare_edit_sidebar_for_entry()
        self._view_controller.show_edit_view()
        self._start_transition_animation(entering=True, splitter_start_sizes=splitter_sizes)

        self.editingStarted.emit(source)

    def leave_edit_mode(self, animate: bool = True) -> None:
        """Return to the standard detail view, optionally animating the transition."""

        self._cancel_pending_previews()
        if self._transition_direction == "exit":
            return
        if not self._view_controller.is_edit_view_active() and self._transition_direction != "enter":
            return

        # Ensure the preview surface shows the latest adjusted frame before any widgets start
        # disappearing so the user never sees a partially restored original.
        self._handle_compare_released()

        # Relax the navigation sidebar constraints and capture the edit sidebar's live geometry
        # prior to hiding the edit stack so the exit animation starts from the widths the user
        # last observed on-screen.
        self._prepare_navigation_sidebar_for_exit()
        # Capture the edit sidebar's live geometry prior to hiding the edit stack so the exit
        # animation (or the immediate geometry jump in the no-animation path) starts from the
        # on-screen width that the user observed during editing.
        self._prepare_edit_sidebar_for_exit()

        # Keep the edit page visible while the transition plays so the splitter can push the
        # preview surface smoothly.  Cross-fade the headers instead of swapping the stacked
        # widget immediately to avoid the visual "jump" reported by the user.
        self._ui.detail_chrome_container.show()
        if animate:
            self._detail_header_opacity.setOpacity(0.0)
            self._edit_header_opacity.setOpacity(1.0)
        else:
            self._detail_header_opacity.setOpacity(1.0)
            self._edit_header_opacity.setOpacity(0.0)
        self._ui.edit_header_container.show()

        self._start_transition_animation(entering=False, animate=animate)

    # ------------------------------------------------------------------
    def _handle_session_changed(self, values: dict) -> None:
        del values  # Unused – the session already stores the authoritative mapping.
        # Debounce preview updates to avoid recalculating the entire image for
        # every incremental slider movement event.
        self._preview_update_timer.start()

    def _cancel_pending_previews(self) -> None:
        """Stop timers and invalidate outstanding preview work."""

        self._preview_update_timer.stop()
        # Incrementing the job identifier causes any in-flight worker results to
        # be ignored once they finish.
        self._preview_job_id += 1

        # ``_PreviewWorker`` instances keep a strong reference to the signals
        # object that in turn owns the connections back to this controller.  The
        # global thread pool does not offer a way to cancel queued runnables, so
        # we proactively sever those connections and drop our strong references.
        # Doing so ensures the worker can finish at its leisure without keeping
        # the edit controller, the preview session, or large intermediate images
        # alive indefinitely.
        for worker in list(self._active_preview_workers):
            signals = getattr(worker, "_signals", None)
            if signals is not None:
                try:
                    # Disconnect the completion signal from the slot on this
                    # controller.  Qt raises ``TypeError`` when the link was
                    # never created and ``RuntimeError`` when it has already
                    # been severed, so both cases are silently ignored.
                    signals.finished.disconnect(self._on_preview_ready)
                except (TypeError, RuntimeError):
                    pass
                # Mark the signal helper for deletion on the GUI thread once
                # control returns to the event loop.  This avoids destroying
                # QObject instances from worker threads.
                signals.deleteLater()
                # Drop the back-reference from the worker to the signal helper
                # so Python's garbage collector can reclaim both objects once
                # the worker finishes executing.
                setattr(worker, "_signals", None)
            # Allow the QRunnable to clean itself up after ``run`` completes so
            # the thread pool does not retain it longer than necessary.
            worker.setAutoDelete(True)
        # Clearing the set removes our strong references which breaks the final
        # reference cycle.  Any worker that is still running will complete and
        # be destroyed automatically without holding on to the controller.
        self._active_preview_workers.clear()

    def _start_preview_job(self) -> None:
        """Queue a background task that recalculates the preview image."""

        if self._preview_session is None or self._session is None:
            self._ui.edit_image_viewer.clear()
            return

        self._preview_job_id += 1
        job_id = self._preview_job_id

        signals = _PreviewSignals()
        signals.finished.connect(self._on_preview_ready)

        if self._preview_backend.supports_realtime:
            # Hardware accelerated backends are fast enough to run synchronously
            # on the UI thread, so we render immediately and forward the result.
            try:
                image = self._preview_backend.render(
                    self._preview_session,
                    self._session.values(),
                )
            except Exception:
                image = QImage()
            self._on_preview_ready(image, job_id)
            return

        worker = _PreviewWorker(
            self._preview_backend,
            self._preview_session,
            self._session.values(),
            job_id,
            signals,
        )
        self._active_preview_workers.add(worker)
        signals.finished.connect(lambda *_: self._active_preview_workers.discard(worker))

        # Submitting the worker to the shared thread pool keeps resource usage
        # bounded even when the user adjusts multiple sliders rapidly.
        self._thread_pool.start(worker)

    def _prepare_preview_image(self, image: QImage) -> QImage:
        """Return an image optimised for preview rendering throughput.

        Applying adjustments to a 1:1 copy of the source file quickly becomes
        prohibitively expensive for high resolution assets.  The edit preview
        only needs to match the on-screen size, so the helper scales the source
        to the current viewer dimensions (or a conservative fallback) while
        preserving the aspect ratio.  The reduced pixel count keeps CPU based
        rendering responsive without sacrificing perceived quality.
        """

        viewport_size = None
        viewer = self._ui.edit_image_viewer

        # ``ImageViewer`` exposes its scroll area viewport for external event
        # filters.  Reusing that helper yields the exact drawable surface size
        # when the widget has already been laid out.
        if hasattr(viewer, "viewport_widget"):
            try:
                viewport = viewer.viewport_widget()
            except Exception:
                viewport = None
            if viewport is not None:
                size = viewport.size()
                if size.isValid() and not size.isEmpty():
                    viewport_size = size

        if viewport_size is None:
            size = viewer.size()
            if size.isValid() and not size.isEmpty():
                viewport_size = size

        # Fall back to a 1600px bounding box when layout information is not yet
        # available (for example the first time the edit view is opened).  The
        # limit is high enough to look crisp on typical displays while avoiding
        # the worst case performance hit of processing multi-tens-of-megapixel
        # originals on the CPU.
        max_width = 1600
        max_height = 1600
        if viewport_size is not None:
            max_width = max(1, viewport_size.width())
            max_height = max(1, viewport_size.height())

        if image.width() <= max_width and image.height() <= max_height:
            # The source already fits within the requested bounds.  Return a
            # detached copy so subsequent pixel operations never touch the
            # caller's instance.
            return QImage(image)

        return image.scaled(
            max_width,
            max_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _on_preview_ready(self, image: QImage, job_id: int) -> None:
        """Update the preview if the emitted job matches the latest request."""

        if job_id != self._preview_job_id:
            # A newer preview superseded this result.  Drop it silently so the
            # UI reflects the most recent slider state.
            return

        if image.isNull():
            self._current_preview_pixmap = None
            self._ui.edit_image_viewer.clear()
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._current_preview_pixmap = None
            self._ui.edit_image_viewer.clear()
            return

        self._current_preview_pixmap = pixmap
        if not self._compare_active:
            self._ui.edit_image_viewer.set_pixmap(pixmap)

    def _handle_compare_pressed(self) -> None:
        """Display the original photo while the compare button is held."""

        if self._base_image is None:
            return
        self._compare_active = True
        self._ui.edit_image_viewer.set_pixmap(QPixmap.fromImage(self._base_image))

    def _handle_compare_released(self) -> None:
        """Restore the adjusted preview after a comparison glance."""

        self._compare_active = False
        if self._current_preview_pixmap is not None and not self._current_preview_pixmap.isNull():
            self._ui.edit_image_viewer.set_pixmap(self._current_preview_pixmap)
        elif self._base_image is not None:
            # Fall back to the unadjusted preview if a recalculated frame is not available.
            self._ui.edit_image_viewer.set_pixmap(QPixmap.fromImage(self._base_image))

    def _handle_reset_clicked(self) -> None:
        if self._session is None:
            return
        # Stop any pending preview updates so the reset renders immediately.
        self._cancel_pending_previews()
        self._session.reset()

    def _handle_done_clicked(self) -> None:
        # Ensure no delayed preview runs after committing the adjustments.
        self._cancel_pending_previews()
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

    def _set_mode(self, mode: str) -> None:
        if mode == "adjust":
            self._ui.edit_adjust_action.setChecked(True)
            self._ui.edit_crop_action.setChecked(False)
            self._ui.edit_sidebar.set_mode("adjust")
        else:
            self._ui.edit_adjust_action.setChecked(False)
            self._ui.edit_crop_action.setChecked(True)
            self._ui.edit_sidebar.set_mode("crop")

    def _prepare_edit_sidebar_for_entry(self) -> None:
        """Collapse the edit sidebar before playing the entrance animation."""

        sidebar = self._ui.edit_sidebar
        sidebar.show()
        sidebar.setMinimumWidth(0)
        sidebar.setMaximumWidth(0)
        sidebar.updateGeometry()

    def _prepare_navigation_sidebar_for_entry(self) -> None:
        """Relax the album sidebar so it can collapse without jumping."""

        sidebar = self._ui.sidebar
        sidebar.relax_minimum_width_for_animation()
        sidebar.updateGeometry()

    def _prepare_edit_sidebar_for_exit(self) -> None:
        """Relax sidebar constraints so it can collapse smoothly when leaving edit mode."""

        sidebar = self._ui.edit_sidebar
        sidebar.show()
        sidebar.setMinimumWidth(0)
        starting_width = sidebar.width()
        # ``QPropertyAnimation`` inspects the target property's current value when the
        # animation starts.  Because ``maximumWidth`` was previously relaxed to a very
        # large sentinel during edit mode (allowing the user to resize the pane), using
        # that limit as the starting point would yield a value such as ``16777215``.  The
        # animation would then appear to "jump" closed immediately because Qt clamps the
        # oversized range on the first frame.  Capturing the live geometry ensures the
        # slide-out begins from the actual on-screen width that the user sees.
        sidebar.setMaximumWidth(int(starting_width))
        sidebar.updateGeometry()

    def _prepare_navigation_sidebar_for_exit(self) -> None:
        """Allow the album sidebar to expand from zero width during the exit animation."""

        sidebar = self._ui.sidebar
        sidebar.relax_minimum_width_for_animation()
        sidebar.updateGeometry()

    def _start_transition_animation(
        self,
        *,
        entering: bool,
        splitter_start_sizes: list[int] | None = None,
        animate: bool = True,
    ) -> None:
        """Animate the splitter, edit sidebar, and header cross-fade."""

        if self._transition_group is not None:
            # Stop any in-flight transition so the new animation starts from the current
            # geometry instead of the previous animation's goal state.
            self._transition_group.stop()
            self._transition_group.deleteLater()
            self._transition_group = None
            self._transition_direction = None

        splitter = self._ui.splitter
        if splitter_start_sizes is None:
            splitter_start_sizes = self._sanitise_splitter_sizes(splitter.sizes())
        total = sum(splitter_start_sizes)
        if total <= 0:
            total = max(1, splitter.width())

        # Preserve a single code path for animated and instant transitions.  Qt treats a
        # zero-duration animation as "apply the end state immediately" while still emitting
        # the usual ``finished`` signal, which keeps the controller's cleanup logic identical.
        duration = 250 if animate else 0

        if entering:
            splitter_end_sizes = self._sanitise_splitter_sizes([0, total], total=total)
            sidebar_start = 0
            sidebar_end = min(self._edit_sidebar_preferred_width, self._edit_sidebar_maximum_width)
        else:
            previous_sizes = self._splitter_sizes_before_edit or []
            splitter_end_sizes = self._sanitise_splitter_sizes(previous_sizes, total=total)
            if not splitter_end_sizes:
                # Fall back to a 25/75 split that mirrors the typical navigation layout.
                fallback_left = max(int(total * 0.25), 1)
                splitter_end_sizes = self._sanitise_splitter_sizes([fallback_left, total - fallback_left], total=total)
            sidebar_start = self._ui.edit_sidebar.width()
            sidebar_end = 0

        sidebar_start = int(sidebar_start)
        sidebar_end = int(sidebar_end)

        animation_group = QParallelAnimationGroup(self)
        splitter_animation = QPropertyAnimation(
            self._splitter_animation_adapter,
            b"sizes",
            animation_group,
        )
        splitter_animation.setDuration(duration)
        splitter_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        splitter_animation.setStartValue(splitter_start_sizes)
        splitter_animation.setEndValue(splitter_end_sizes)

        animation_group.addAnimation(splitter_animation)

        sidebar_animation = QPropertyAnimation(
            self._ui.edit_sidebar,
            b"maximumWidth",
            animation_group,
        )
        sidebar_animation.setDuration(duration)
        sidebar_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        sidebar_animation.setStartValue(sidebar_start)
        sidebar_animation.setEndValue(sidebar_end)
        animation_group.addAnimation(sidebar_animation)

        if not entering:
            edit_header_fade = QPropertyAnimation(
                self._edit_header_opacity,
                b"opacity",
                animation_group,
            )
            edit_header_fade.setDuration(duration)
            edit_header_fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
            edit_header_fade.setStartValue(self._edit_header_opacity.opacity())
            edit_header_fade.setEndValue(0.0)
            animation_group.addAnimation(edit_header_fade)

            detail_header_fade = QPropertyAnimation(
                self._detail_header_opacity,
                b"opacity",
                animation_group,
            )
            detail_header_fade.setDuration(duration)
            detail_header_fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
            detail_header_fade.setStartValue(self._detail_header_opacity.opacity())
            detail_header_fade.setEndValue(1.0)
            animation_group.addAnimation(detail_header_fade)

        animation_group.finished.connect(self._on_transition_finished)

        self._transition_direction = "enter" if entering else "exit"
        self._transition_group = animation_group
        if duration == 0:
            # Immediate transitions should update the UI synchronously so callers can assume the
            # state reflects the requested mode change as soon as this method returns.
            self._splitter_animation_adapter.sizes = splitter_end_sizes
            self._ui.edit_sidebar.setMaximumWidth(sidebar_end)
            if entering:
                self._ui.edit_sidebar.setMinimumWidth(self._edit_sidebar_minimum_width)
                self._ui.edit_sidebar.updateGeometry()
            else:
                self._edit_header_opacity.setOpacity(0.0)
                self._detail_header_opacity.setOpacity(1.0)
            self._on_transition_finished()
            return

        animation_group.start()

    def _on_transition_finished(self) -> None:
        """Reset widget constraints and clean up after an animated transition."""

        direction = self._transition_direction
        if self._transition_group is not None:
            self._transition_group.deleteLater()
            self._transition_group = None
        self._transition_direction = None

        if direction == "enter":
            self._finalise_enter_transition()
        elif direction == "exit":
            self._finalise_exit_transition()

    def _finalise_enter_transition(self) -> None:
        """Restore the edit sidebar's normal width constraints after sliding in."""

        sidebar = self._ui.edit_sidebar
        sidebar.setMinimumWidth(self._edit_sidebar_minimum_width)
        sidebar.setMaximumWidth(self._edit_sidebar_maximum_width)
        sidebar.updateGeometry()

        navigation_sidebar = self._ui.sidebar
        # Restore the navigation sidebar constraints at the same time as the edit sidebar so the
        # next transition begins from a consistent geometry state.  Leaving the minimum width at
        # zero would cause the splitter to snap open instantly when we attempt to expand it during
        # the exit animation, recreating the "flash" that prompted this fix.
        navigation_sidebar.restore_minimum_width_after_animation()
        navigation_sidebar.updateGeometry()

        # The splitter already reached the collapsed configuration via the property animation, so
        # no additional geometry adjustments are required here.

    def _finalise_exit_transition(self) -> None:
        """Tear down the edit UI once the slide-out animation finishes."""

        splitter = self._ui.splitter
        target_sizes: list[int] | None = None
        if self._splitter_sizes_before_edit:
            # Normalise the cached geometry against the splitter's current width so the restored
            # sizes respect any window resize that happened while the edit tools were visible.
            total_width = max(1, splitter.width())
            target_sizes = self._sanitise_splitter_sizes(
                self._splitter_sizes_before_edit,
                total=total_width,
            )

        navigation_sidebar = self._ui.sidebar
        navigation_sidebar.restore_minimum_width_after_animation()
        navigation_sidebar.updateGeometry()

        sidebar = self._ui.edit_sidebar
        sidebar.hide()
        sidebar.setMinimumWidth(0)
        sidebar.setMaximumWidth(0)
        sidebar.updateGeometry()

        # With the animation complete it is safe to switch the stacked widget back to the detail
        # view.  Doing so earlier would compress the edit page mid-animation, producing the
        # "jump" the user reported.  Restoring the shared toolbar widgets at the same time keeps
        # the controls consistent with the now-visible header.
        self._restore_header_widgets_after_edit()
        self._view_controller.show_detail_view()
        self._ui.detail_chrome_container.show()
        self._detail_header_opacity.setOpacity(1.0)

        self._ui.edit_header_container.hide()
        self._edit_header_opacity.setOpacity(1.0)

        if target_sizes:
            current_sizes = [int(value) for value in splitter.sizes()]
            # Reapply the saved layout only when the animation failed to reach the expected end
            # state (for example after a zero-duration transition or when Qt clamps the values
            # because a pane temporarily disappears).  Skipping the redundant ``setSizes`` call
            # prevents the navigation sidebar from snapping at the end of an otherwise smooth
            # animation while still guaranteeing the geometry recovers after editing.
            if len(current_sizes) != len(target_sizes) or any(
                abs(current - expected) > 1 for current, expected in zip(current_sizes, target_sizes)
            ):
                splitter.setSizes(target_sizes)

        self._ui.edit_sidebar.set_session(None)
        self._ui.edit_image_viewer.clear()

        self._session = None
        self._base_image = None
        self._current_source = None
        self._current_preview_pixmap = None
        self._compare_active = False
        if self._preview_session is not None:
            self._preview_backend.dispose_session(self._preview_session)
            self._preview_session = None
        self._splitter_sizes_before_edit = None

    def _sanitise_splitter_sizes(
        self,
        sizes,
        *,
        total: int | None = None,
    ) -> list[int]:
        """Clamp *sizes* to the splitter child count and normalise their sum."""

        splitter = self._ui.splitter
        count = splitter.count()
        if count == 0:
            return []
        try:
            raw = [int(value) for value in sizes] if sizes is not None else []
        except TypeError:
            raw = []
        if len(raw) < count:
            raw.extend(0 for _ in range(count - len(raw)))
        elif len(raw) > count:
            raw = raw[:count]
        sanitised = [max(0, value) for value in raw]
        current_total = sum(sanitised)
        if total is None or total <= 0:
            total = current_total if current_total > 0 else max(1, splitter.width())
        if current_total <= 0:
            # Distribute the available space evenly to keep the splitter geometry stable.
            base = total // count
            sanitised = [base] * count
            if sanitised:
                sanitised[-1] += total - base * count
            return sanitised
        if current_total == total:
            return sanitised
        scaled: list[int] = []
        accumulated = 0
        for index, value in enumerate(sanitised):
            if index == count - 1:
                scaled_value = total - accumulated
            else:
                scaled_value = int(round(value * total / current_total))
                accumulated += scaled_value
            scaled.append(max(0, scaled_value))
        difference = total - sum(scaled)
        if scaled and difference != 0:
            scaled[-1] += difference
        return scaled

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
        ui.zoom_widget.hide()

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

