"""State container for the non-destructive editing workflow."""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Iterable, Mapping

from PySide6.QtCore import QObject, Signal

from ....core.light_resolver import LIGHT_KEYS
from ....core.color_resolver import COLOR_KEYS, COLOR_RANGES, ColorStats


class EditSession(QObject):
    """Hold the adjustment values for the active editing session."""

    valueChanged = Signal(str, object)
    """Emitted when a single adjustment changes."""

    valuesChanged = Signal(dict)
    """Emitted after one or more adjustments have been updated."""

    resetPerformed = Signal()
    """Emitted when :meth:`reset` restores every adjustment to its default."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._values: "OrderedDict[str, float | bool]" = OrderedDict()
        self._ranges: dict[str, tuple[float, float]] = {}
        self._color_stats: ColorStats | None = None
        # The master slider value feeds the resolver that generates the derived light adjustments.
        self._values["Light_Master"] = 0.0
        self._ranges["Light_Master"] = (-1.0, 1.0)
        # ``Light_Enabled`` toggles whether the resolved adjustments should be applied.  Storing the
        # state alongside the numeric adjustments keeps the session serialisable through
        # :meth:`values` without coordinating multiple containers.
        self._values["Light_Enabled"] = True
        self._ranges["Light_Enabled"] = (-1.0, 1.0)
        for key in LIGHT_KEYS:
            self._values[key] = 0.0
            self._ranges[key] = (-1.0, 1.0)

        self._values["Color_Master"] = 0.0
        self._ranges["Color_Master"] = (-1.0, 1.0)
        self._values["Color_Enabled"] = True
        self._ranges["Color_Enabled"] = (-1.0, 1.0)
        for key in COLOR_KEYS:
            self._values[key] = 0.0
            self._ranges[key] = (-1.0, 1.0)

        # ``BW_*`` parameters drive the GPU-only black & white pass.  Intensity and grain operate in
        # the ``[0.0, 1.0]`` range while the other controls are symmetric to allow both warm and
        # cool adjustments.  ``BW_Master`` stores the aggregate slider position so the UI can
        # reproduce the derived parameters after a round-trip through the sidecar.  ``BW_Enabled``
        # mirrors the Light/Color toggles so the UI can disable the effect without discarding the
        # user tuned slider values.
        self._values["BW_Master"] = 0.0
        self._ranges["BW_Master"] = (0.0, 1.0)
        self._values["BW_Enabled"] = True
        self._ranges["BW_Enabled"] = (0.0, 1.0)
        self._values["BW_Intensity"] = 0.0
        self._ranges["BW_Intensity"] = (0.0, 1.0)
        self._values["BW_Neutrals"] = 0.0
        self._ranges["BW_Neutrals"] = (-1.0, 1.0)
        self._values["BW_Tone"] = 0.0
        self._ranges["BW_Tone"] = (-1.0, 1.0)
        self._values["BW_Grain"] = 0.0
        self._ranges["BW_Grain"] = (0.0, 1.0)

    # ------------------------------------------------------------------
    # Accessors
    def value(self, key: str) -> float | bool:
        """Return the stored value for *key*, defaulting to ``0.0`` or ``False``."""

        return self._values.get(key, 0.0)

    def values(self) -> Dict[str, float | bool]:
        """Return a shallow copy of every stored adjustment."""

        return dict(self._values)

    # ------------------------------------------------------------------
    # Mutation helpers
    def set_value(self, key: str, value) -> None:
        """Update *key* with *value* while honouring the stored type."""

        if key not in self._values:
            return
        current = self._values[key]
        if isinstance(current, bool):
            normalised = bool(value)
            if normalised is current:
                return
        else:
            minimum, maximum = self._ranges.get(key, (-1.0, 1.0))
            normalised = max(minimum, min(maximum, float(value)))
            if abs(normalised - float(current)) < 1e-4:
                return
        self._values[key] = normalised
        self.valueChanged.emit(key, normalised)
        self.valuesChanged.emit(self.values())

    def set_values(self, updates: Mapping[str, float | bool], *, emit_individual: bool = True) -> None:
        """Update multiple *updates* at once."""

        changed: list[tuple[str, float | bool]] = []
        for key, value in updates.items():
            if key not in self._values:
                continue
            current = self._values[key]
            if isinstance(current, bool):
                normalised = bool(value)
                if normalised is current:
                    continue
            else:
                minimum, maximum = self._ranges.get(key, (-1.0, 1.0))
                normalised = max(minimum, min(maximum, float(value)))
                if abs(normalised - float(current)) < 1e-4:
                    continue
            self._values[key] = normalised
            changed.append((key, normalised))
        if not changed:
            return
        if emit_individual:
            for key, value in changed:
                self.valueChanged.emit(key, value)
        self.valuesChanged.emit(self.values())

    def reset(self) -> None:
        """Restore the master and fine-tuning adjustments to their defaults."""

        defaults: dict[str, float | bool] = {
            "Light_Master": 0.0,
            "Light_Enabled": True,
            "Color_Master": 0.0,
            "Color_Enabled": True,
        }
        defaults.update({key: 0.0 for key in LIGHT_KEYS})
        defaults.update({key: 0.0 for key in COLOR_KEYS})
        defaults.update({
            "BW_Master": 0.0,
            "BW_Enabled": True,
            "BW_Intensity": 0.0,
            "BW_Neutrals": 0.0,
            "BW_Tone": 0.0,
            "BW_Grain": 0.0,
        })
        self.set_values(defaults, emit_individual=True)
        self.resetPerformed.emit()

    # ------------------------------------------------------------------
    def set_color_stats(self, stats: ColorStats | None) -> None:
        """Persist *stats* for use by Color adjustment resolvers."""

        self._color_stats = stats

    def color_stats(self) -> ColorStats | None:
        """Return the most recently assigned :class:`ColorStats` instance."""

        return self._color_stats

    # ------------------------------------------------------------------
    # Convenience helpers used by tests and controllers
    def load_from_mapping(self, mapping: Mapping[str, float]) -> None:
        """Replace the current state using *mapping* while emitting signals."""

        self.set_values(mapping, emit_individual=True)

    def iter_items(self) -> Iterable[tuple[str, float]]:
        """Yield the adjustment keys in their canonical order."""

        return self._values.items()
