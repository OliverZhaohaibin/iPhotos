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
            self.currentChanged.emit(row)
            if source is not None:
                self.sourceChanged.emit(source)
            return source
        self._previous_row = self._current_row
        self._current_row = row
        source = self._resolve_source(row)
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
            self.currentChanged.emit(-1)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _on_model_changed(self, *args, **kwargs) -> None:  # pragma: no cover - Qt signature noise
        """React to structural model updates and keep playback focused.

        The playlist listens for insert/remove/reset notifications to ensure the
        currently focused asset always refers to a valid proxy row.  When a row
        disappears (for example, because a favourite flag was toggled while the
        *Favorites* filter is active) ``_current_row`` may suddenly point past
        the end of the model.  Previously the controller cleared the selection
        outright which forced the detail view back to the gallery.  The updated
        logic attempts to select the closest surviving row before giving up so
        playback continues seamlessly whenever another asset is available.
        """

        if self._model is None:
            return
        if self._current_row == -1:
            return

        row_count = self._model.rowCount()
        if row_count == 0:
            self.clear()
            return

        if 0 <= self._current_row < row_count and self._is_playable(self._current_row):
            return

        fallback = self._resolve_surviving_row(row_count)
        if fallback is None:
            self.clear()
            return

        # ``set_current`` updates ``_previous_row`` and emits the usual
        # signals, ensuring that view controllers stay in sync with the new
        # selection.
        self.set_current(fallback)

    def _resolve_surviving_row(self, row_count: int) -> int | None:
        """Return the nearest playable row after a removal.

        The search favours the item that visually follows the removed entry so
        that unfavouriting a photo in the *Favorites* view naturally reveals the
        next favourite.  If no later rows survive we walk backwards towards the
        start of the model.  ``None`` indicates that no playable rows remain.
        """

        if self._model is None or row_count <= 0:
            return None

        # Clamp the starting position so it always references an existing row.
        start = min(max(self._current_row, 0), row_count - 1)
        if self._is_playable(start):
            return start

        # Scan forwards first so the replacement mirrors the behaviour of the
        # gallery grid, where the next thumbnail automatically slides into the
        # vacated position.
        for forward in range(start + 1, row_count):
            if self._is_playable(forward):
                return forward

        # Fallback to the nearest previous row when nothing follows the removed
        # item.  This keeps the most recent neighbour in view.
        for backward in range(start - 1, -1, -1):
            if self._is_playable(backward):
                return backward

        return None

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
