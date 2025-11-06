"""Manage edit view transition animations independently from theme handling."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import (
    QObject,
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QVariantAnimation,
    Signal,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect

from ..ui_main_window import Ui_MainWindow
from .edit_theme_manager import EditThemeManager

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from .detail_ui_controller import DetailUIController


class EditViewTransitionManager(QObject):
    """Coordinate edit view animations while delegating theme changes."""

    transition_finished = Signal(str)
    """Emitted with ``"enter"`` or ``"exit"`` once an animation completes."""

    def __init__(
        self,
        ui: Ui_MainWindow,
        window: QObject | None,
        parent: Optional[QObject] = None,
        *,
        theme_manager: EditThemeManager | None = None,
    ) -> None:
        """Initialise animations and capture the theme manager dependency."""

        super().__init__(parent)
        self._ui = ui
        self._theme_manager = theme_manager or EditThemeManager(ui, window)

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

        self._edit_header_opacity = QGraphicsOpacityEffect(ui.edit_header_container)
        self._edit_header_opacity.setOpacity(1.0)
        ui.edit_header_container.setGraphicsEffect(self._edit_header_opacity)

        self._detail_header_opacity = QGraphicsOpacityEffect(ui.detail_chrome_container)
        self._detail_header_opacity.setOpacity(1.0)
        ui.detail_chrome_container.setGraphicsEffect(self._detail_header_opacity)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_detail_ui_controller(
        self, controller: "DetailUIController" | None
    ) -> None:
        """Record *controller* so icon tinting tracks the active theme."""

        self._theme_manager.set_detail_ui_controller(controller)

    def is_transition_active(self) -> bool:
        """Return ``True`` if an enter/exit animation is running."""

        return self._transition_group is not None or self._transition_direction is not None

    def enter_edit_mode(self, animate: bool = True) -> None:
        """Apply the edit chrome and animate the sidebar into view."""

        if self._transition_direction == "enter":
            return

        self._detail_header_opacity.setOpacity(1.0)
        self._ui.detail_chrome_container.hide()
        self._ui.edit_header_container.show()
        self._edit_header_opacity.setOpacity(1.0)
        self._ui.filmstrip_view.hide()

        splitter_sizes = self._sanitise_splitter_sizes(self._ui.splitter.sizes())
        self._splitter_sizes_before_edit = list(splitter_sizes)
        self._prepare_navigation_sidebar_for_entry()
        self._prepare_edit_sidebar_for_entry()
        self._theme_manager.apply_dark_theme()
        self._start_transition_animation(
            entering=True,
            splitter_start_sizes=splitter_sizes,
            animate=animate,
        )

    def leave_edit_mode(self, animate: bool = True, *, show_filmstrip: bool = True) -> None:
        """Restore the standard chrome and animate the sidebar out of view.

        Parameters
        ----------
        animate:
            When ``True`` the sidebar collapse is animated; ``False`` snaps
            everything back immediately which is useful for error recovery.
        show_filmstrip:
            Controls whether the filmstrip is made visible again once edit mode
            finishes.  Callers can pass ``False`` when the user's preferences
            request the filmstrip remain hidden on the detail page.
        """

        if self._transition_direction == "exit":
            return

        self._prepare_navigation_sidebar_for_exit()
        self._prepare_edit_sidebar_for_exit()
        self._theme_manager.restore_light_theme()

        self._ui.detail_chrome_container.show()
        self._ui.edit_header_container.show()
        if show_filmstrip:
            self._ui.filmstrip_view.show()
        else:
            # Ensure the filmstrip stays hidden when the user's settings request
            # it.  Calling ``hide`` keeps the widget state consistent even if it
            # was temporarily shown by other UI interactions while edit mode
            # was active.
            self._ui.filmstrip_view.hide()
        if animate:
            self._detail_header_opacity.setOpacity(0.0)
            self._edit_header_opacity.setOpacity(1.0)
        else:
            self._detail_header_opacity.setOpacity(1.0)
            self._edit_header_opacity.setOpacity(0.0)

        self._start_transition_animation(entering=False, animate=animate)

    # ------------------------------------------------------------------
    # Transition helpers
    # ------------------------------------------------------------------
    def _prepare_edit_sidebar_for_entry(self) -> None:
        sidebar = self._ui.edit_sidebar
        sidebar.show()
        sidebar.setMinimumWidth(0)
        sidebar.setMaximumWidth(0)
        sidebar.updateGeometry()

    def _prepare_navigation_sidebar_for_entry(self) -> None:
        sidebar = self._ui.sidebar
        sidebar.relax_minimum_width_for_animation()
        sidebar.updateGeometry()

    def _prepare_edit_sidebar_for_exit(self) -> None:
        sidebar = self._ui.edit_sidebar
        sidebar.show()
        starting_width = sidebar.width()
        sidebar.setMinimumWidth(int(starting_width))
        sidebar.setMaximumWidth(int(starting_width))
        sidebar.updateGeometry()

    def _prepare_navigation_sidebar_for_exit(self) -> None:
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
        if self._transition_group is not None:
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

        duration = 250 if animate else 0

        if entering:
            splitter_end_sizes = self._sanitise_splitter_sizes([0, total], total=total)
            sidebar_start = 0
            sidebar_end = min(self._edit_sidebar_preferred_width, self._edit_sidebar_maximum_width)
        else:
            previous_sizes = self._splitter_sizes_before_edit or []
            splitter_end_sizes = self._sanitise_splitter_sizes(previous_sizes, total=total)
            if not splitter_end_sizes:
                fallback_left = max(int(total * 0.25), 1)
                splitter_end_sizes = self._sanitise_splitter_sizes(
                    [fallback_left, total - fallback_left],
                    total=total,
                )
            sidebar_start = self._ui.edit_sidebar.width()
            sidebar_end = 0

        sidebar_start = int(sidebar_start)
        sidebar_end = int(sidebar_end)

        shell, shell_start_color, shell_end_color = self._theme_manager.get_shell_animation_colors(
            entering
        )

        if shell is not None and shell_start_color is not None:
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
        if len(start_sizes) < pane_count:
            start_sizes.extend(0 for _ in range(pane_count - len(start_sizes)))
        if len(end_sizes) < pane_count:
            end_sizes.extend(0 for _ in range(pane_count - len(end_sizes)))

        def _apply_splitter_progress(value: float) -> None:
            progress = max(0.0, min(1.0, float(value)))
            interpolated: list[int] = []
            accumulated = 0
            for index in range(pane_count):
                start = start_sizes[index]
                end = end_sizes[index]
                raw = start + (end - start) * progress
                if index == pane_count - 1:
                    rounded = max(0, total - accumulated)
                else:
                    rounded = max(0, int(round(raw)))
                    accumulated += rounded
                interpolated.append(rounded)
            splitter.setSizes(interpolated)

        splitter_animation.valueChanged.connect(_apply_splitter_progress)

        def _apply_final_sizes() -> None:
            splitter.setSizes(end_sizes[:pane_count])

        splitter_animation.finished.connect(_apply_final_sizes)
        animation_group.addAnimation(splitter_animation)

        def _add_sidebar_dimension_animation(property_name: bytes) -> None:
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

        _add_sidebar_dimension_animation(b"minimumWidth")
        _add_sidebar_dimension_animation(b"maximumWidth")

        if shell is not None and shell_start_color is not None and shell_end_color is not None:
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
        direction = self._transition_direction
        if self._transition_group is not None:
            self._transition_group.deleteLater()
            self._transition_group = None
        self._transition_direction = None

        if direction == "enter":
            self._finalise_enter_transition()
        elif direction == "exit":
            self._finalise_exit_transition()

        if direction:
            self.transition_finished.emit(direction)

    def _finalise_enter_transition(self) -> None:
        sidebar = self._ui.edit_sidebar
        sidebar.setMinimumWidth(self._edit_sidebar_minimum_width)
        sidebar.setMaximumWidth(self._edit_sidebar_maximum_width)
        sidebar.updateGeometry()

    def _finalise_exit_transition(self) -> None:
        splitter = self._ui.splitter
        target_sizes: list[int] | None = None
        if self._splitter_sizes_before_edit:
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
            if len(current_sizes) != len(target_sizes) or any(
                abs(current - expected) > 1 for current, expected in zip(current_sizes, target_sizes)
            ):
                splitter.setSizes(target_sizes)

        self._ui.detail_chrome_container.show()
        self._detail_header_opacity.setOpacity(1.0)

        self._ui.edit_header_container.hide()
        self._edit_header_opacity.setOpacity(1.0)

        self._splitter_sizes_before_edit = None

    def _sanitise_splitter_sizes(
        self,
        sizes,
        *,
        total: int | None = None,
    ) -> list[int]:
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
