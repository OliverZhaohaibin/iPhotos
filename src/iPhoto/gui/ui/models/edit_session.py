"""State container for the non-destructive editing workflow."""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Iterable, Mapping

from PySide6.QtCore import QObject, Signal

from ....core.image_filters import LIGHT_KEYS


class EditSession(QObject):
    """Hold the adjustment values for the active editing session."""

    valueChanged = Signal(str, float)
    """Emitted when a single adjustment changes."""

    valuesChanged = Signal(dict)
    """Emitted after one or more adjustments have been updated."""

    resetPerformed = Signal()
    """Emitted when :meth:`reset` restores every adjustment to its default."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._values: "OrderedDict[str, float]" = OrderedDict(
            (key, 0.0) for key in LIGHT_KEYS
        )

    # ------------------------------------------------------------------
    # Accessors
    def value(self, key: str) -> float:
        """Return the stored value for *key*, defaulting to ``0.0``."""

        return float(self._values.get(key, 0.0))

    def values(self) -> Dict[str, float]:
        """Return a shallow copy of every stored adjustment."""

        return dict(self._values)

    # ------------------------------------------------------------------
    # Mutation helpers
    def set_value(self, key: str, value: float) -> None:
        """Update *key* with *value* clamped to ``[-1.0, 1.0]``."""

        if key not in self._values:
            return
        clamped = max(-1.0, min(1.0, float(value)))
        current = self._values[key]
        if abs(clamped - current) < 1e-4:
            return
        self._values[key] = clamped
        self.valueChanged.emit(key, clamped)
        self.valuesChanged.emit(self.values())

    def set_values(self, updates: Mapping[str, float], *, emit_individual: bool = True) -> None:
        """Update multiple *updates* at once."""

        changed: list[tuple[str, float]] = []
        for key, value in updates.items():
            if key not in self._values:
                continue
            clamped = max(-1.0, min(1.0, float(value)))
            current = self._values[key]
            if abs(clamped - current) < 1e-4:
                continue
            self._values[key] = clamped
            changed.append((key, clamped))
        if not changed:
            return
        if emit_individual:
            for key, value in changed:
                self.valueChanged.emit(key, value)
        self.valuesChanged.emit(self.values())

    def reset(self) -> None:
        """Restore every adjustment to ``0.0``."""

        self.set_values({key: 0.0 for key in self._values}, emit_individual=True)
        self.resetPerformed.emit()

    # ------------------------------------------------------------------
    # Convenience helpers used by tests and controllers
    def load_from_mapping(self, mapping: Mapping[str, float]) -> None:
        """Replace the current state using *mapping* while emitting signals."""

        self.set_values(mapping, emit_individual=True)

    def iter_items(self) -> Iterable[tuple[str, float]]:
        """Yield the adjustment keys in their canonical order."""

        return self._values.items()
