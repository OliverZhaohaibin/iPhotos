"""Playback playlist coordination for the asset grid."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QModelIndex, Signal

from ..models.asset_model import AssetModel, Roles


class PlaylistController(QObject):
    """Coordinate playback order for assets exposed via :class:`AssetModel`."""

    currentChanged = Signal(int)
    sourceChanged = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._model: Optional[AssetModel] = None
        self._current_row: int = -1
        self._previous_row: int = -1
        # ``_current_rel`` mirrors the currently selected asset's relative path.
        # Remembering the identifier allows the controller to restore or
        # migrate the selection when the proxy model reorders itself (for
        # example when the Favorites filter drops an item), keeping the detail
        # view focused on a sensible neighbour instead of falling back to the
        # gallery.
        self._current_rel: Optional[str] = None

    # ------------------------------------------------------------------
    # Model wiring
    # ------------------------------------------------------------------
    def bind_model(self, model: AssetModel) -> None:
        """Attach *model* as the playlist source."""

        if self._model is not None:
            self._model.modelReset.disconnect(self._on_model_changed)
            self._model.rowsInserted.disconnect(self._on_model_changed)
            self._model.rowsRemoved.disconnect(self._on_model_changed)
        self._model = model
        model.modelReset.connect(self._on_model_changed)
        model.rowsInserted.connect(self._on_model_changed)
        model.rowsRemoved.connect(self._on_model_changed)
        self._on_model_changed()

    # ------------------------------------------------------------------
    # Playlist navigation
    # ------------------------------------------------------------------
    def set_current(self, row: int) -> Optional[Path]:
        """Select *row* as the active playback item."""

        if self._model is None:
            return None
        if not (0 <= row < self._model.rowCount()):
            return None
        if not self._is_playable(row):
            return None
        if row == self._current_row:
            source = self._resolve_source(row)
            self._current_rel = self._resolve_rel(row)
            self.currentChanged.emit(row)
            if source is not None:
                self.sourceChanged.emit(source)
            return source
        self._previous_row = self._current_row
        self._current_row = row
        source = self._resolve_source(row)
        self._current_rel = self._resolve_rel(row)
        self.currentChanged.emit(row)
        if source is not None:
            self.sourceChanged.emit(source)
        return source

    def next(self) -> Optional[Path]:
        """Advance to the next playable asset in the model."""

        return self._step(1)

    def previous(self) -> Optional[Path]:
        """Go back to the previous playable asset in the model."""

        return self._step(-1)

    def current_row(self) -> int:
        """Return the currently selected row, or ``-1`` when unset."""

        return self._current_row

    def current_source(self) -> Optional[Path]:
        """Return the :class:`Path` of the active media item, if any."""

        if self._current_row == -1:
            return None
        return self._resolve_source(self._current_row)

    def previous_row(self) -> int:
        """Return the previously active row, or ``-1`` if unavailable."""

        return self._previous_row

    def clear(self) -> None:
        """Reset the controller to an empty state."""

        if self._current_row != -1:
            self._previous_row = self._current_row
            self._current_row = -1
            self._current_rel = None
            self.currentChanged.emit(-1)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _on_model_changed(self, *args, **kwargs) -> None:  # pragma: no cover - Qt signature noise
        if self._model is None:
            return
        if self._current_row == -1:
            return
        count = self._model.rowCount()
        if count == 0:
            self.clear()
            return

        if 0 <= self._current_row < count:
            # If the proxy index stayed within range, confirm that it still
            # references the same asset.  Filter changes (such as removing an
            # item from the Favorites view) can reuse the same row number for a
            # different asset once the model compacts itself.  In that case the
            # playback controller needs to emit an updated selection so the
            # detail pane refreshes accordingly.
            current_rel = self._resolve_rel(self._current_row)
            if current_rel == self._current_rel and current_rel is not None:
                return
            # Attempt to relocate the original asset elsewhere in the model so
            # the selection survives reordering.  When the asset truly
            # disappears (for example, it leaves the Favorites filter) fall
            # back to a nearby surrogate.
            if self._current_rel:
                relocated = self._find_row_by_rel(self._current_rel)
                if relocated is not None:
                    self.set_current(relocated)
                    return
            self._select_surrogate_row(count)
            return

        # ``_current_row`` fell outside the proxy bounds, meaning the
        # previously focused asset vanished altogether.  Promote the nearest
        # remaining row to keep the detail pane populated.
        self._select_surrogate_row(count)

    def _step(self, delta: int) -> Optional[Path]:
        if self._model is None or self._model.rowCount() == 0:
            return None
        count = self._model.rowCount()
        if self._current_row == -1:
            if delta > 0:
                start = 0
                end = count
                step = 1
            else:
                start = count - 1
                end = -1
                step = -1
        else:
            if delta > 0:
                start = self._current_row + 1
                end = count
                step = 1
            else:
                start = self._current_row - 1
                end = -1
                step = -1
        for row in range(start, end, step):
            if self._is_playable(row):
                return self.set_current(row)
        return None

    def _is_playable(self, row: int) -> bool:
        """Return ``True`` when *row* is within range of the bound model."""

        if self._model is None:
            return False
        return 0 <= row < self._model.rowCount()

    def _resolve_source(self, row: int) -> Optional[Path]:
        if self._model is None:
            return None
        index: QModelIndex = self._model.index(row, 0)
        if bool(index.data(Roles.IS_LIVE)):
            motion_abs = index.data(Roles.LIVE_MOTION_ABS)
            if isinstance(motion_abs, str) and motion_abs:
                return Path(motion_abs)
            motion_rel = index.data(Roles.LIVE_MOTION_REL)
            if isinstance(motion_rel, str) and motion_rel:
                source_model = self._model.source_model()
                album_root = source_model.album_root()
                if album_root is not None:
                    return (album_root / motion_rel).resolve()
            return None
        raw = index.data(Roles.ABS)
        if isinstance(raw, str) and raw:
            return Path(raw)
        return None

    def _resolve_rel(self, row: int) -> Optional[str]:
        """Return the relative path associated with *row*, if available."""

        if self._model is None:
            return None
        index: QModelIndex = self._model.index(row, 0)
        if not index.isValid():
            return None
        rel = index.data(Roles.REL)
        return str(rel) if isinstance(rel, str) else None

    def _find_row_by_rel(self, rel: str) -> Optional[int]:
        """Locate the proxy row that currently exposes *rel*, if any."""

        if self._model is None:
            return None
        count = self._model.rowCount()
        for row in range(count):
            if self._resolve_rel(row) == rel:
                return row
        return None

    def _select_surrogate_row(self, count: int) -> None:
        """Pick the nearest playable row so the detail pane stays populated."""

        if self._model is None or count <= 0:
            self.clear()
            return
        anchor = min(max(self._current_row, 0), count - 1)
        tried: set[int] = set()

        def _consider(row: int) -> bool:
            if row in tried or row < 0 or row >= count:
                return False
            tried.add(row)
            if self._is_playable(row):
                self.set_current(row)
                return True
            return False

        if _consider(anchor):
            return
        for candidate in range(anchor - 1, -1, -1):
            if _consider(candidate):
                return
        for candidate in range(anchor + 1, count):
            if _consider(candidate):
                return
        self.clear()
