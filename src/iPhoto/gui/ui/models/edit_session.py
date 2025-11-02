"""State container for the non-destructive editing workflow."""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Iterable, Mapping

from PySide6.QtCore import QObject, Signal

from ....core.light_resolver import LIGHT_KEYS


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
        # The master slider value feeds the resolver that generates the derived light adjustments.
        self._values["Light_Master"] = 0.0
        # ``Light_Enabled`` toggles whether the resolved adjustments should be applied.  Storing the
        # state alongside the numeric adjustments keeps the session serialisable through
        # :meth:`values` without coordinating multiple containers.
        self._values["Light_Enabled"] = True
        for key in LIGHT_KEYS:
            self._values[key] = 0.0

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
            normalised = max(-1.0, min(1.0, float(value)))
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
                normalised = max(-1.0, min(1.0, float(value)))
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
        }
        defaults.update({key: 0.0 for key in LIGHT_KEYS})
        self.set_values(defaults, emit_individual=True)
        self.resetPerformed.emit()

    # ------------------------------------------------------------------
    # Convenience helpers used by tests and controllers
    def load_from_mapping(self, mapping: Mapping[str, float]) -> None:
        """Replace the current state using *mapping* while emitting signals."""

        self.set_values(mapping, emit_individual=True)

    def iter_items(self) -> Iterable[tuple[str, float]]:
        """Yield the adjustment keys in their canonical order."""

        return self._values.items()
