"""Dedicated full screen workflow management for the edit view."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Optional

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget

from ...utils import image_loader
from ..ui_main_window import Ui_MainWindow
from .edit_preview_manager import EditPreviewManager

_LOGGER = logging.getLogger(__name__)


class EditFullscreenManager(QObject):
    """Handle immersive full screen transitions for the edit image viewer."""

    def __init__(
        self,
        ui: Ui_MainWindow,
        window: Optional[QObject],
        preview_manager: EditPreviewManager,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._ui = ui
        self._window: Optional[QObject] = window
        self._preview_manager = preview_manager

        # Track whether the immersive layout is currently active so callers can
        # avoid re-entering the workflow while a session is already running.
        self._fullscreen_active = False
        # Remember which chrome widgets were visible before entering full
        # screen.  This lets the manager restore the exact state the user had
        # configured (for example a hidden sidebar) when the immersive mode is
        # closed again.
        self._fullscreen_hidden_widgets: list[tuple[QWidget, bool]] = []
        # Persist the splitter geometry so the navigation and edit sidebars can
        # return to their previous sizes after the edit viewer releases the
        # entire window back to the standard layout.
        self._fullscreen_splitter_sizes: list[int] | None = None
        # Record the edit sidebar's width constraints so we can temporarily
        # relax them and then reinstate the user's customisation when exiting.
        self._fullscreen_edit_sidebar_constraints: tuple[int, int] | None = None

    # ------------------------------------------------------------------
    # Public API used by :class:`EditController`
    # ------------------------------------------------------------------
    def is_in_fullscreen(self) -> bool:
        """Return ``True`` if the immersive edit full screen mode is active."""

        return self._fullscreen_active

    def enter_fullscreen_preview(
        self,
        source: Path,
        adjustments: Mapping[str, float],
    ) -> bool:
        """Expand the edit viewer to fill the window using *adjustments*.

        Parameters
        ----------
        source:
            The asset currently being edited.  The manager loads this path at
            full resolution to ensure the immersive preview looks crisp.
        adjustments:
            The latest non-destructive adjustment mapping emitted by the edit
            session.  This is forwarded to :class:`EditPreviewManager` so the
            full resolution preview matches the edit view state.

        Returns
        -------
        bool
            ``True`` when the transition succeeds and the viewer now occupies
            the entire window.  ``False`` indicates the workflow was aborted
            (for example because the image failed to load).
        """

        if self._fullscreen_active:
            return False
        if not isinstance(self._window, QWidget):
            return False

        full_res_image = image_loader.load_qimage(source)
        if full_res_image is None or full_res_image.isNull():
            _LOGGER.warning("Failed to load full resolution image for %s", source)
            return False

        try:
            initial_pixmap = self._preview_manager.start_session(
                full_res_image,
                adjustments,
                scale_for_viewport=False,
            )
        except Exception:  # pragma: no cover - safety net mirrors legacy guard
            _LOGGER.warning(
                "Failed to initialise full screen preview session for %s",
                source,
            )
            return False

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

        edit_sidebar.setMinimumWidth(0)
        edit_sidebar.setMaximumWidth(0)
        edit_sidebar.updateGeometry()

        navigation_sidebar = self._ui.sidebar
        relax_navigation = getattr(
            navigation_sidebar,
            "relax_minimum_width_for_animation",
            None,
        )
        if callable(relax_navigation):
            relax_navigation()

        splitter = self._ui.splitter
        self._fullscreen_splitter_sizes = self._sanitise_splitter_sizes(
            splitter.sizes()
        )
        total = sum(self._fullscreen_splitter_sizes or [])
        if total <= 0:
            total = max(1, splitter.width())
        splitter.setSizes([0, total])

        self._window.showFullScreen()

        self._fullscreen_active = True

        if initial_pixmap.isNull():
            self._ui.edit_image_viewer.clear()
        else:
            self._ui.edit_image_viewer.set_pixmap(initial_pixmap)
        self._ui.edit_image_viewer.reset_zoom()

        return True

    def exit_fullscreen_preview(
        self,
        source: Optional[Path],
        adjustments: Optional[Mapping[str, float]],
    ) -> bool:
        """Restore the standard edit chrome and preview session.

        Parameters
        ----------
        source:
            The asset that should back the restored preview session.  ``None``
            indicates the controller no longer has an active edit.
        adjustments:
            The latest adjustment mapping to reapply when recreating the
            standard preview session.  ``None`` mirrors the scenario where the
            edit session has already been torn down.

        Returns
        -------
        bool
            ``True`` when the immersive layout has been dismantled.  The return
            value does not guarantee that a new preview session could be
            created; callers should continue to handle fallbacks in that case.
        """

        if not self._fullscreen_active:
            return False
        if not isinstance(self._window, QWidget):
            return False

        self._preview_manager.cancel_pending_updates()
        self._window.showNormal()

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

        if source is None or adjustments is None:
            self._preview_manager.stop_session()
            return True

        source_image = self._preview_manager.get_base_image()
        if source_image is None or source_image.isNull():
            source_image = image_loader.load_qimage(source)

        if source_image is None or source_image.isNull():
            self._preview_manager.stop_session()
            self._ui.edit_image_viewer.clear()
            return True

        try:
            initial_pixmap = self._preview_manager.start_session(
                source_image,
                adjustments,
                scale_for_viewport=True,
            )
        except Exception:  # pragma: no cover - defensive logging mirrors legacy
            _LOGGER.warning(
                "Failed to restore standard preview session for %s",
                source,
            )
            self._preview_manager.stop_session()
            return True

        if initial_pixmap.isNull():
            self._ui.edit_image_viewer.clear()
        else:
            self._ui.edit_image_viewer.set_pixmap(initial_pixmap)
        self._ui.edit_image_viewer.reset_zoom()

        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _sanitise_splitter_sizes(
        self,
        sizes,
        *,
        total: int | None = None,
    ) -> list[int]:
        """Normalise a ``QSplitter`` size list for re-use after transitions.

        Qt's :meth:`~PySide6.QtWidgets.QSplitter.sizes` method returns a list
        that may not match the exact number of child widgets, especially while
        an animation is running.  This helper mirrors the behaviour originally
        implemented on :class:`EditController` so the immersive workflow can
        safely cache the geometry, even when invoked mid-transition.
        """

        # Convert the iterable to a list of integers so we can freely adjust
        # individual entries without mutating any Qt-managed buffers.
        normalised = [int(value) for value in sizes]

        # Ensure the list length matches the splitter child count.  Qt may
        # report more entries than expected, so truncate, or too few entries, so
        # pad the missing values with zeros.
        child_count = self._ui.splitter.count()
        if len(normalised) > child_count:
            normalised = normalised[:child_count]
        else:
            normalised.extend(0 for _ in range(child_count - len(normalised)))

        if total is None:
            total = sum(normalised)
            if total <= 0:
                total = max(1, self._ui.splitter.width())

        # Clamp negative values that can appear when a widget is temporarily
        # hidden.  The immersive flow needs a positive geometry to avoid Qt
        # raising warnings when restoring the splitter state.
        normalised = [max(0, value) for value in normalised]

        if not normalised:
            return [total]

        current_total = sum(normalised)
        if current_total <= 0:
            # If everything collapsed to zero we hand the entire width to the
            # last widget, which matches the legacy behaviour.
            normalised[-1] = total
            return normalised

        # Scale the values proportionally so they add up to ``total``.  This
        # preserves the user's layout ratios while ensuring the splitter accepts
        # the restored sizes without additional adjustments.
        scale = total / float(current_total)
        scaled = [max(0, int(round(value * scale))) for value in normalised]

        # Rounding can introduce off-by-one errors, so adjust the final element
        # to consume any remaining pixels and keep Qt satisfied.
        delta = total - sum(scaled)
        if scaled:
            scaled[-1] += delta

        return scaled

