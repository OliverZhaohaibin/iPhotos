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
    Signal,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QImage, QPalette, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from ....core.preview_backends import PreviewBackend, PreviewSession, select_preview_backend
from ....io import sidecar
from ..palette import SIDEBAR_BACKGROUND_COLOR
from ..icon import load_icon
from ...utils import image_loader
from ..models.asset_model import AssetModel
from ..models.edit_session import EditSession
from ..tasks.thumbnail_loader import ThumbnailLoader
from ..ui_main_window import Ui_MainWindow
from ..widgets.collapsible_section import CollapsibleSection
from ..window_manager import RoundedWindowShell
from .player_view_controller import PlayerViewController
from .view_controller import ViewController

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from .detail_ui_controller import DetailUIController
    from .navigation_controller import NavigationController


_LOGGER = logging.getLogger(__name__)


_EDIT_DARK_STYLESHEET = "\n".join(
    [
        "QWidget#editPage {",
        "  background-color: #1C1C1E;",
        "}",
        "QWidget#editPage QLabel,",
        "QWidget#editPage QToolButton,",
        "QWidget#editHeaderContainer QPushButton {",
        "  color: #F5F5F7;",
        "}",
        "QWidget#editHeaderContainer {",
        "  background-color: #2C2C2E;",
        "  border-radius: 12px;",
        "}",
        "QWidget#editPage EditSidebar,",
        "QWidget#editPage EditSidebar QWidget,",
        "QWidget#editPage QScrollArea,",
        "QWidget#editPage QScrollArea > QWidget {",
        "  background-color: #2C2C2E;",
        "  color: #F5F5F7;",
        "}",
        "QWidget#editPage QGroupBox {",
        "  background-color: #1F1F1F;",
        "  border: 1px solid #323236;",
        "  border-radius: 10px;",
        "  margin-top: 24px;",
        "  padding-top: 12px;",
        "}",
        "QWidget#editPage QGroupBox::title {",
        "  color: #F5F5F7;",
        "  subcontrol-origin: margin;",
        "  left: 12px;",
        "  padding: 0 4px;",
        "}",
        "QWidget#editPage #collapsibleSection QLabel {",
        "  color: #F5F5F7;",
        "}",
    ]
)


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
        self._default_edit_page_stylesheet = ui.edit_page.styleSheet()
        # Cache the original style sheets so the chrome returns to the light theme verbatim.
        self._default_sidebar_stylesheet = ui.sidebar.styleSheet()
        self._default_statusbar_stylesheet = ui.status_bar.styleSheet()
        self._default_window_chrome_stylesheet = ui.window_chrome.styleSheet()
        self._default_window_shell_stylesheet = ui.window_shell.styleSheet()
        self._default_title_bar_stylesheet = ui.title_bar.styleSheet()
        self._default_title_separator_stylesheet = ui.title_separator.styleSheet()
        self._default_menu_bar_container_stylesheet = ui.menu_bar_container.styleSheet()
        self._default_menu_bar_stylesheet = ui.menu_bar.styleSheet()
        self._default_rescan_button_stylesheet = ui.rescan_button.styleSheet()

        # ``RoundedWindowShell`` owns the antialiased frame that produces the
        # macOS-style rounded corners.  Record a reference so the dark edit
        # theme can tint the shell directly without forcing the interior
        # ``window_shell`` widget to draw an opaque rectangle that would square
        # off the corners.
        shell_parent = ui.window_shell.parentWidget()
        self._rounded_window_shell: RoundedWindowShell | None = (
            shell_parent if isinstance(shell_parent, RoundedWindowShell) else None
        )

        # Remember the light-theme palettes so we can reinstate them after leaving edit mode.
        self._default_sidebar_palette = QPalette(ui.sidebar.palette())
        self._default_statusbar_palette = QPalette(ui.status_bar.palette())
        self._default_window_chrome_palette = QPalette(ui.window_chrome.palette())
        self._default_window_shell_palette = QPalette(ui.window_shell.palette())
        self._default_title_bar_palette = QPalette(ui.title_bar.palette())
        self._default_title_separator_palette = QPalette(ui.title_separator.palette())
        self._default_menu_bar_container_palette = QPalette(ui.menu_bar_container.palette())
        self._default_menu_bar_palette = QPalette(ui.menu_bar.palette())
        self._default_rescan_button_palette = QPalette(ui.rescan_button.palette())
        self._default_selection_button_palette = QPalette(ui.selection_button.palette())
        self._default_selection_button_stylesheet = ui.selection_button.styleSheet()
        self._default_window_title_palette = QPalette(ui.window_title_label.palette())
        self._default_window_title_stylesheet = ui.window_title_label.styleSheet()
        self._default_sidebar_tree_palette = QPalette(ui.sidebar._tree.palette())
        self._default_statusbar_message_palette = QPalette(ui.status_bar._message_label.palette())

        # Persist the original auto-fill flags to avoid forcing opaque backgrounds in light mode.
        self._default_sidebar_autofill = ui.sidebar.autoFillBackground()
        self._default_statusbar_autofill = ui.status_bar.autoFillBackground()
        self._default_window_chrome_autofill = ui.window_chrome.autoFillBackground()
        self._default_window_shell_autofill = ui.window_shell.autoFillBackground()
        self._default_title_bar_autofill = ui.title_bar.autoFillBackground()
        self._default_title_separator_autofill = ui.title_separator.autoFillBackground()
        self._default_menu_bar_container_autofill = ui.menu_bar_container.autoFillBackground()
        self._default_menu_bar_autofill = ui.menu_bar.autoFillBackground()
        self._default_rescan_button_autofill = ui.rescan_button.autoFillBackground()
        self._default_sidebar_tree_autofill = ui.sidebar._tree.autoFillBackground()

        # Preserve the rounded shell's palette and colour override so the
        # custom frame returns to whatever appearance the frameless window
        # manager configured (for example immersive mode) after leaving edit
        # mode.
        if self._rounded_window_shell is not None:
            self._default_rounded_shell_palette = QPalette(
                self._rounded_window_shell.palette()
            )
            self._default_rounded_shell_override: QColor | None = getattr(
                self._rounded_window_shell, "_override_color", None
            )
        else:
            self._default_rounded_shell_palette = None
            self._default_rounded_shell_override = None
        self._edit_theme_applied = False

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
        if self._detail_ui_controller is not None:
            self._detail_ui_controller.disconnect_zoom_controls()
        self._connect_edit_zoom_controls()
        self._ui.edit_image_viewer.reset_zoom()
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

    def _apply_edit_dark_theme(self) -> None:
        """Activate the dark edit palette across the entire window chrome."""

        if self._edit_theme_applied:
            return
        self._ui.edit_page.setStyleSheet(_EDIT_DARK_STYLESHEET)
        self._ui.edit_image_viewer.set_surface_color_override("#111111")

        # Recolour key edit controls so their icons match the bright foreground text used in dark
        # mode.  The icons are reloaded because QIcon caches do not automatically respond to
        # palette changes.
        # Use a bright white tint so icons remain legible against the dark header chrome.
        dark_icon_color = QColor("#FFFFFF")
        dark_icon_hex = dark_icon_color.name(QColor.NameFormat.HexArgb)
        self._ui.edit_compare_button.setIcon(
            load_icon(
                "square.fill.and.line.vertical.and.square.svg",
                color=dark_icon_hex,
            )
        )
        for section in self._ui.edit_sidebar.findChildren(CollapsibleSection):
            toggle_button = getattr(section, "_toggle_button", None)
            if toggle_button is not None:
                toggle_icon = (
                    "chevron.down.svg" if section.is_expanded() else "chevron.right.svg"
                )
                toggle_button.setIcon(load_icon(toggle_icon, color=dark_icon_hex))
            icon_label = getattr(section, "_icon_label", None)
            icon_name = getattr(section, "_icon_name", "")
            if icon_label is not None and icon_name:
                icon_label.setPixmap(
                    load_icon(icon_name, color=dark_icon_hex).pixmap(20, 20)
                )

        # Match the zoom controls to the dark chrome so the +/- affordances stay legible.
        self._ui.zoom_out_button.setIcon(load_icon("minus.svg", color=dark_icon_hex))
        self._ui.zoom_in_button.setIcon(load_icon("plus.svg", color=dark_icon_hex))

        # Ask the detail controller to tint the info and favourite icons if it is available.
        if self._detail_ui_controller is not None:
            self._detail_ui_controller.set_toolbar_icon_tint(dark_icon_color)
        else:
            # Fallback for tests where the detail controller is not wired yet.  Tint both buttons
            # directly so the edit toolbar still offers sufficient contrast in isolated harnesses.
            self._ui.info_button.setIcon(
                load_icon("info.circle.svg", color=dark_icon_hex)
            )
            self._ui.favorite_button.setIcon(
                load_icon("suit.heart.svg", color=dark_icon_hex)
            )

        # Construct a palette that mirrors macOS Photos' edit chrome so each widget picks up the
        # same deep greys and bright foreground colours.
        # Centralising the palette avoids a maze of bespoke style sheets and keeps the visuals
        # coherent.
        dark_palette = QPalette()
        window_color = QColor("#1C1C1E")
        button_color = QColor("#2C2C2E")
        text_color = QColor("#F5F5F7")
        disabled_text = QColor("#7F7F7F")
        accent_color = QColor("#0A84FF")
        outline_color = QColor("#323236")
        placeholder_text = QColor(245, 245, 247, 160)

        dark_palette.setColor(QPalette.ColorRole.Window, window_color)
        dark_palette.setColor(QPalette.ColorRole.Base, window_color)
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#242426"))
        dark_palette.setColor(QPalette.ColorRole.WindowText, text_color)
        dark_palette.setColor(QPalette.ColorRole.Text, text_color)
        dark_palette.setColor(QPalette.ColorRole.Button, button_color)
        dark_palette.setColor(QPalette.ColorRole.ButtonText, text_color)
        dark_palette.setColor(QPalette.ColorRole.BrightText, QColor("#FFFFFF"))
        dark_palette.setColor(QPalette.ColorRole.Link, accent_color)
        dark_palette.setColor(QPalette.ColorRole.Highlight, QColor("#3A3A3C"))
        dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        dark_palette.setColor(QPalette.ColorRole.PlaceholderText, placeholder_text)
        dark_palette.setColor(QPalette.ColorRole.Mid, outline_color)
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, button_color)
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, text_color)
        dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.ButtonText,
            disabled_text,
        )
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.WindowText,
            disabled_text,
        )
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.Highlight,
            QColor("#2C2C2E"),
        )
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.HighlightedText,
            disabled_text,
        )
        dark_palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.PlaceholderText,
            QColor(160, 160, 160, 160),
        )

        widgets_to_update = [
            self._ui.sidebar,
            self._ui.status_bar,
            self._ui.window_chrome,
            self._ui.menu_bar_container,
            self._ui.menu_bar,
            self._ui.title_bar,
            self._ui.title_separator,
        ]
        for widget in widgets_to_update:
            widget.setPalette(dark_palette)
            # Chrome widgets need to stay transparent so the rounded host widget can continue
            # painting the curved outline.  ``setAutoFillBackground(False)`` prevents Qt from
            # rasterising an opaque rectangle that would obscure the shell.
            widget.setAutoFillBackground(False)

        # Mirror the palette adjustment for the standalone Rescan button so the control inherits
        # the same foreground colours as the surrounding chrome while keeping its background
        # transparent for the rounded shell.
        self._ui.rescan_button.setPalette(dark_palette)
        self._ui.rescan_button.setAutoFillBackground(False)

        # ``window_shell`` must remain transparent so the rounded host widget
        # can paint the curved edge.  Update its palette but leave auto-fill
        # disabled so the shell does not overwrite the frame with an opaque
        # rectangle.
        self._ui.window_shell.setPalette(dark_palette)
        self._ui.window_shell.setAutoFillBackground(False)

        if self._rounded_window_shell is not None:
            self._rounded_window_shell.setPalette(dark_palette)
            self._rounded_window_shell.set_override_color(window_color)

        # Forward the palette to nested labels and the album tree so text,
        # disclosure indicators, and menu captions adopt the same foreground colour.
        self._ui.sidebar._tree.setPalette(dark_palette)
        self._ui.sidebar._tree.setAutoFillBackground(False)
        self._ui.status_bar._message_label.setPalette(dark_palette)
        self._ui.selection_button.setPalette(dark_palette)
        self._ui.window_title_label.setPalette(dark_palette)

        # Refresh the frameless window manager's menu palette before overriding chrome styles so the
        # global ``QMenu`` stylesheet tracks the active theme while the menu bar remains transparent.
        self._refresh_menu_styles()
        self._ui.menu_bar.setAutoFillBackground(False)

        # Replace the light-theme style sheets while keeping the chrome transparent.  The palette
        # now supplies the foreground colours, so only the text metrics and highlight accents are
        # overridden explicitly.  Leaving the background transparent allows the rounded shell to
        # show through and maintain its corner treatment.
        foreground_color = text_color.name()
        accent_color_name = accent_color.name()
        outline_color_name = outline_color.name()
        self._ui.sidebar.setStyleSheet(
            "\n".join(
                [
                    "QWidget#albumSidebar {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QWidget#albumSidebar QLabel {",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        self._ui.status_bar.setStyleSheet(
            "\n".join(
                [
                    "QWidget#chromeStatusBar {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QWidget#chromeStatusBar QLabel {",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        self._ui.title_bar.setStyleSheet(
            "\n".join(
                [
                    "QWidget#windowTitleBar {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QWidget#windowTitleBar QLabel {",
                    f"  color: {foreground_color};",
                    "}",
                    "QWidget#windowTitleBar QToolButton {",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        self._ui.title_separator.setStyleSheet(
            "QFrame#windowTitleSeparator {"
            f"  background-color: {outline_color_name};"
            "  border: none;"
            "}"
        )
        self._ui.menu_bar.setStyleSheet(
            "\n".join(
                [
                    "QMenuBar#chromeMenuBar {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                    "QMenuBar#chromeMenuBar::item {",
                    f"  color: {foreground_color};",
                    "}",
                    "QMenuBar#chromeMenuBar::item:selected {",
                    f"  background-color: {outline_color_name};",
                    "  border-radius: 6px;",
                    "}",
                    "QMenuBar#chromeMenuBar::item:pressed {",
                    f"  background-color: {accent_color_name};",
                    "}",
                ]
            )
        )
        self._ui.menu_bar_container.setStyleSheet(
            "\n".join(
                [
                    "QWidget#menuBarContainer {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        self._ui.rescan_button.setStyleSheet(
            "\n".join(
                [
                    "QToolButton#rescanButton {",
                    "  background-color: transparent;",
                    f"  color: {foreground_color};",
                    "}",
                ]
            )
        )
        # ``window_chrome`` does not expose an object name, so rely on its top-level selector to
        # enforce the transparent background and shared foreground tint.
        self._ui.window_chrome.setStyleSheet(
            "\n".join(
                [
                    "background-color: transparent;",
                    f"color: {foreground_color};",
                ]
            )
        )

        self._edit_theme_applied = True

    def _restore_edit_theme(self) -> None:
        """Restore the default light theme after leaving edit mode."""

        if not self._edit_theme_applied:
            return
        self._ui.edit_page.setStyleSheet(self._default_edit_page_stylesheet)
        self._ui.edit_image_viewer.set_surface_color_override(None)

        # Restore the untinted icons now that the interface has returned to the light theme.
        self._ui.edit_compare_button.setIcon(
            load_icon("square.fill.and.line.vertical.and.square.svg")
        )
        for section in self._ui.edit_sidebar.findChildren(CollapsibleSection):
            toggle_button = getattr(section, "_toggle_button", None)
            if toggle_button is not None:
                toggle_icon = (
                    "chevron.down.svg" if section.is_expanded() else "chevron.right.svg"
                )
                toggle_button.setIcon(load_icon(toggle_icon))
            icon_label = getattr(section, "_icon_label", None)
            icon_name = getattr(section, "_icon_name", "")
            if icon_label is not None and icon_name:
                icon_label.setPixmap(load_icon(icon_name).pixmap(20, 20))

        # Return the zoom affordances and shared toolbar buttons to their light theme assets.
        self._ui.zoom_out_button.setIcon(load_icon("minus.svg"))
        self._ui.zoom_in_button.setIcon(load_icon("plus.svg"))
        if self._detail_ui_controller is not None:
            self._detail_ui_controller.set_toolbar_icon_tint(None)
        else:
            self._ui.info_button.setIcon(load_icon("info.circle.svg"))
            self._ui.favorite_button.setIcon(load_icon("suit.heart.svg"))

        widgets_to_restore = [
            (
                self._ui.sidebar,
                self._default_sidebar_palette,
                self._default_sidebar_autofill,
            ),
            (
                self._ui.status_bar,
                self._default_statusbar_palette,
                self._default_statusbar_autofill,
            ),
            (
                self._ui.window_chrome,
                self._default_window_chrome_palette,
                self._default_window_chrome_autofill,
            ),
            (
                self._ui.window_shell,
                self._default_window_shell_palette,
                self._default_window_shell_autofill,
            ),
            (
                self._ui.menu_bar_container,
                self._default_menu_bar_container_palette,
                self._default_menu_bar_container_autofill,
            ),
            (
                self._ui.menu_bar,
                self._default_menu_bar_palette,
                self._default_menu_bar_autofill,
            ),
            (
                self._ui.rescan_button,
                self._default_rescan_button_palette,
                self._default_rescan_button_autofill,
            ),
            (
                self._ui.title_bar,
                self._default_title_bar_palette,
                self._default_title_bar_autofill,
            ),
            (
                self._ui.title_separator,
                self._default_title_separator_palette,
                self._default_title_separator_autofill,
            ),
        ]
        for widget, palette, autofill in widgets_to_restore:
            widget.setPalette(QPalette(palette))
            widget.setAutoFillBackground(autofill)

        self._ui.sidebar._tree.setPalette(QPalette(self._default_sidebar_tree_palette))
        self._ui.sidebar._tree.setAutoFillBackground(self._default_sidebar_tree_autofill)
        self._ui.status_bar._message_label.setPalette(QPalette(self._default_statusbar_message_palette))
        self._ui.selection_button.setPalette(QPalette(self._default_selection_button_palette))
        # The selection toggle sits beside ``rescan_button`` in the chrome row, so it needs the
        # same stylesheet reset to drop the temporary dark-mode foreground override.
        self._apply_color_reset_stylesheet(
            self._ui.selection_button,
            self._default_selection_button_stylesheet,
            "QToolButton#selectionButton",
        )
        self._ui.window_title_label.setPalette(QPalette(self._default_window_title_palette))
        # Restore the window title to its light theme colour without guessing the palette value.
        self._apply_color_reset_stylesheet(
            self._ui.window_title_label,
            self._default_window_title_stylesheet,
            "QLabel#windowTitleLabel",
        )

        # Update the global menu stylesheet ahead of reinstating the cached chrome styles.  This
        # ensures popup menus follow the restored light palette while still allowing the widgets to
        # return to their original appearance.
        self._refresh_menu_styles()
        self._ui.menu_bar.setAutoFillBackground(self._default_menu_bar_autofill)

        # Restore the original style sheets alongside the palettes so light mode reappears exactly
        # as it was before entering edit mode.  ``or`` fallbacks guard against empty strings for the
        # sidebar, which historically relied on a constant background colour.
        self._ui.sidebar.setStyleSheet(
            self._default_sidebar_stylesheet
            or (
                "QWidget#albumSidebar {\n"
                f"    background-color: {SIDEBAR_BACKGROUND_COLOR.name()};\n"
                "}"
            )
        )
        self._ui.status_bar.setStyleSheet(self._default_statusbar_stylesheet)
        self._ui.window_chrome.setStyleSheet(self._default_window_chrome_stylesheet)
        self._ui.window_shell.setStyleSheet(self._default_window_shell_stylesheet)
        self._ui.title_bar.setStyleSheet(self._default_title_bar_stylesheet)
        self._ui.title_separator.setStyleSheet(self._default_title_separator_stylesheet)
        # Restore the chrome row hosting the menu bar and Rescan button so it returns to its light
        # theme appearance precisely as captured before entering edit mode.
        self._ui.menu_bar_container.setStyleSheet(
            self._default_menu_bar_container_stylesheet
        )
        self._ui.menu_bar.setStyleSheet(self._default_menu_bar_stylesheet)
        self._ui.rescan_button.setStyleSheet(self._default_rescan_button_stylesheet)

        if self._rounded_window_shell is not None:
            if self._default_rounded_shell_palette is not None:
                self._rounded_window_shell.setPalette(
                    QPalette(self._default_rounded_shell_palette)
                )
            self._rounded_window_shell.set_override_color(
                self._default_rounded_shell_override
            )

        self._edit_theme_applied = False

    def _apply_color_reset_stylesheet(
        self,
        widget: QWidget,
        cached_stylesheet: str | None,
        selector: str,
    ) -> None:
        """Recombine *widget*'s cached stylesheet with a neutral text colour.

        Dark mode injects high-specificity rules that force white foregrounds
        onto controls embedded in the chrome row.
        Simply restoring the original stylesheet is insufficient because the
        ``color`` attribute remains latched to the dark override.  Appending a
        ``color: unset`` rule targeted at the widget's object name explicitly
        clears that override so Qt falls back to the palette we just restored.

        Parameters
        ----------
        widget:
            The control that should resume using the palette-provided text
            colour (for example the Select button or the window title label).
        cached_stylesheet:
            The stylesheet captured before entering edit mode.  ``None`` or an
            empty string is treated as the absence of an explicit style.
        selector:
            A CSS selector that uniquely identifies *widget*.  Using the object
            name keeps the rule scoped to the relevant control only.
        """

        base_stylesheet = (cached_stylesheet or "").strip()
        reset_stylesheet = "\n".join(
            [
                f"{selector} {{",
                "    color: unset;",
                "}",
            ]
        )
        combined_stylesheet = "\n".join(
            part for part in (base_stylesheet, reset_stylesheet) if part
        )
        widget.setStyleSheet(combined_stylesheet)

    def _refresh_menu_styles(self) -> None:
        """Rebuild the frameless window manager's menu palette if available."""

        if self._window is None:
            return
        window_manager = getattr(self._window, "window_manager", None)
        if window_manager is None:
            return
        apply_styles = getattr(window_manager, "_apply_menu_styles", None)
        if not callable(apply_styles):
            return
        # ``_apply_menu_styles`` adjusts the global ``QMenu`` stylesheet.  Calling it after the
        # palette flips ensures popup menus inherit the correct foreground and background colours
        # without duplicating the logic that already lives in the frameless window manager.
        apply_styles()

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
        starting_width = sidebar.width()
        sidebar.setMinimumWidth(int(starting_width))
        # Keep the minimum width anchored to the live geometry while the animation is prepared.
        # Resetting the constraint to zero here would let the layout reclaim the space instantly,
        # producing the "instant collapse" the user observed before the first animation frame ran.
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

        # Prepare the rounded shell colours ahead of time so the transition can drive a smooth
        # cross-fade between the light and dark palettes.  The palette captured during
        # initialisation represents the light theme baseline, while the dark tone mirrors the edit
        # surface styling.  Computing the start/end values before mutating any palettes guarantees
        # we retain the correct light colour even after the dark palette is applied below.
        shell = self._rounded_window_shell
        shell_start_color: QColor | None = None
        shell_end_color: QColor | None = None
        if shell is not None:
            if self._default_rounded_shell_palette is None:
                # Defensive copy in case the frameless shell was not available during controller
                # construction (for example in tests that stub out the frameless window manager).
                self._default_rounded_shell_palette = QPalette(shell.palette())
            base_palette = self._default_rounded_shell_palette or QPalette(shell.palette())
            light_shell_color = base_palette.color(QPalette.ColorRole.Window)
            dark_shell_color = QColor("#1C1C1E")
            if entering:
                shell_start_color = light_shell_color
                shell_end_color = dark_shell_color
            else:
                shell_start_color = dark_shell_color
                shell_end_color = light_shell_color

        if entering:
            # Flip the chrome widgets to their dark palette before the animation begins so labels
            # and icons stay legible while the window shell fades to black.
            self._apply_edit_dark_theme()

        if shell is not None and shell_start_color is not None:
            # ``_apply_edit_dark_theme`` forces the shell to the dark override immediately.  For the
            # fade effect we reapply the start colour so the animation can interpolate from the
            # captured light tone to the dark tint.
            shell.set_override_color(shell_start_color)

        animation_group = QParallelAnimationGroup(self)

        splitter_animation = QVariantAnimation(animation_group)
        splitter_animation.setDuration(duration)
        splitter_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        splitter_animation.setStartValue(0.0)
        splitter_animation.setEndValue(1.0)

        start_sizes = list(splitter_start_sizes)
        end_sizes = list(splitter_end_sizes)
        pane_count = splitter.count()

        # ``_sanitise_splitter_sizes`` already clamps the arrays to the splitter child count, but
        # pad the copies defensively so the interpolation code below can treat them uniformly even
        # when future UI tweaks add more panes.
        if len(start_sizes) < pane_count:
            start_sizes.extend(0 for _ in range(pane_count - len(start_sizes)))
        if len(end_sizes) < pane_count:
            end_sizes.extend(0 for _ in range(pane_count - len(end_sizes)))

        def _apply_splitter_progress(value: float) -> None:
            """Interpolate the splitter pane widths for the given animation progress."""

            progress = max(0.0, min(1.0, float(value)))
            interpolated: list[int] = []
            accumulated = 0
            for index in range(pane_count):
                start = start_sizes[index]
                end = end_sizes[index]
                raw = start + (end - start) * progress
                if index == pane_count - 1:
                    # Force the final pane to absorb any rounding error so the total width stays
                    # perfectly aligned with the splitter's current geometry.  Without this guard
                    # the animation would occasionally leave a one-pixel gap after the slider
                    # reaches its target value.
                    rounded = max(0, total - accumulated)
                else:
                    rounded = max(0, int(round(raw)))
                    accumulated += rounded
                interpolated.append(rounded)
            splitter.setSizes(interpolated)

        splitter_animation.valueChanged.connect(_apply_splitter_progress)

        def _apply_final_sizes() -> None:
            """Snap the splitter to the exact target sizes once the animation stops."""

            splitter.setSizes(end_sizes[:pane_count])

        splitter_animation.finished.connect(_apply_final_sizes)
        animation_group.addAnimation(splitter_animation)

        def _add_sidebar_dimension_animation(property_name: bytes) -> None:
            """Animate *property_name* so the layout tracks the sidebar width every frame."""

            animation = QPropertyAnimation(
                self._ui.edit_sidebar,
                property_name,
                animation_group,
            )
            animation.setDuration(duration)
            animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
            animation.setStartValue(sidebar_start)
            animation.setEndValue(sidebar_end)
            animation_group.addAnimation(animation)

        # Animate both the minimum and maximum width to identical values.  Keeping the bounds in
        # lockstep ensures Qt's layout engine reallocates space smoothly instead of waiting for the
        # animation to finish before it honours the sidebar's preferred size.
        _add_sidebar_dimension_animation(b"minimumWidth")
        _add_sidebar_dimension_animation(b"maximumWidth")

        if shell is not None and shell_start_color is not None and shell_end_color is not None:
            # Drive the rounded host's tint via a property animation so the frameless chrome fades
            # smoothly between the application themes instead of snapping abruptly.
            shell_animation = QPropertyAnimation(shell, b"overrideColor", animation_group)
            shell_animation.setDuration(duration)
            shell_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
            shell_animation.setStartValue(shell_start_color)
            shell_animation.setEndValue(shell_end_color)
            animation_group.addAnimation(shell_animation)

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
            splitter.setSizes(splitter_end_sizes)
            self._ui.edit_sidebar.setMinimumWidth(sidebar_end)
            self._ui.edit_sidebar.setMaximumWidth(sidebar_end)
            if entering:
                self._ui.edit_sidebar.updateGeometry()
            else:
                self._edit_header_opacity.setOpacity(0.0)
                self._detail_header_opacity.setOpacity(1.0)
            if shell is not None and shell_end_color is not None:
                shell.set_override_color(shell_end_color)
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

        # Intentionally keep the navigation sidebar's relaxed constraints in place while edit mode
        # is active.  The exit transition re-applies the defaults once the user leaves the edit
        # tools, and forcing them here would immediately re-expand the collapsed sidebar.

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

        # With the splitter geometry locked to the target sizes we can safely swap the stacked
        # widget back to the detail page.  Performing the page change earlier gives the newly
        # visible detail view an opportunity to resize the splitter according to its own
        # ``sizeHint`` values, which reintroduces the visual "jump" reported by the user.
        self._disconnect_edit_zoom_controls()
        if self._detail_ui_controller is not None:
            self._detail_ui_controller.connect_zoom_controls()
        self._restore_header_widgets_after_edit()
        self._view_controller.show_detail_view()
        # Restore the light chrome palette only after the detail page becomes visible so the
        # theme change and the page swap occur in the same frame, eliminating the brief flash of
        # dark widgets on the light layout that was noticeable in the previous ordering.
        self._restore_edit_theme()
        self._ui.detail_chrome_container.show()
        self._detail_header_opacity.setOpacity(1.0)

        self._ui.edit_header_container.hide()
        self._edit_header_opacity.setOpacity(1.0)

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

