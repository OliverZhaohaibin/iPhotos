"""Proxy model that injects spacer rows for the filmstrip view."""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractProxyModel,
    QModelIndex,
    QObject,
    Qt,
    QSize,
)

from .roles import Roles


class SpacerProxyModel(QAbstractProxyModel):
    """Wrap an asset model and expose leading/trailing spacer rows."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._spacer_size = QSize(0, 0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_spacer_width(self, width: int) -> None:
        """Update spacer width and notify views when it changes."""

        width = max(0, width)
        if self._spacer_size.width() == width:
            return

        self._spacer_size.setWidth(width)

        # The leading and trailing spacer rows are the only items whose
        # geometry depends on this width. Instead of resetting the entire
        # model (which would force every view to throw away caches and
        # re-query all data), emit targeted ``dataChanged`` signals so
        # views simply refresh the two spacer delegates. This keeps
        # navigation responsive even for very large albums.
        source = self.sourceModel()
        if source is None:
            return

        source_rows = source.rowCount()
        if source_rows <= 0:
            return

        first_idx = self.index(0, 0)
        last_idx = self.index(source_rows + 1, 0)
        roles = [Qt.ItemDataRole.SizeHintRole]
        self.dataChanged.emit(first_idx, first_idx, roles)
        self.dataChanged.emit(last_idx, last_idx, roles)

    # ------------------------------------------------------------------
    # QAbstractProxyModel overrides
    # ------------------------------------------------------------------
    def setSourceModel(self, source_model) -> None:  # type: ignore[override]
        previous = self.sourceModel()
        if previous is not None:
            try:
                previous.modelReset.disconnect(self._handle_source_reset)
                previous.rowsInserted.disconnect(self._handle_source_reset)
                previous.rowsRemoved.disconnect(self._handle_source_reset)
            except (RuntimeError, TypeError):  # pragma: no cover - Qt disconnect noise
                pass

        super().setSourceModel(source_model)

        if source_model is not None:
            source_model.modelReset.connect(self._handle_source_reset)
            source_model.rowsInserted.connect(self._handle_source_reset)
            source_model.rowsRemoved.connect(self._handle_source_reset)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        source = self.sourceModel()
        if source is None:
            return 0
        count = source.rowCount(parent)
        return count + 2 if count > 0 else 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        source = self.sourceModel()
        return source.columnCount(parent) if source is not None else 0

    def mapToSource(self, proxy_index: QModelIndex) -> QModelIndex:  # noqa: N802
        source = self.sourceModel()
        if source is None or not proxy_index.isValid():
            return QModelIndex()
        row = proxy_index.row()
        count = source.rowCount()
        if not (1 <= row <= count):
            return QModelIndex()
        return source.index(row - 1, proxy_index.column())

    def mapFromSource(self, source_index: QModelIndex) -> QModelIndex:  # noqa: N802
        if not source_index.isValid():
            return QModelIndex()
        return self.index(source_index.row() + 1, source_index.column())

    def index(
        self, row: int, column: int, parent: QModelIndex = QModelIndex()
    ) -> QModelIndex:
        if parent.isValid():
            return QModelIndex()
        return self.createIndex(row, column)

    def parent(self, _index: QModelIndex) -> QModelIndex:  # noqa: N802
        return QModelIndex()

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # noqa: N802
        if not index.isValid():
            return None

        source = self.sourceModel()
        if source is None:
            return None

        row = index.row()
        last_row = self.rowCount() - 1
        if row in {0, last_row} and last_row >= 0:
            if role == Roles.IS_SPACER:
                return True
            if role in (Qt.ItemDataRole.SizeHintRole, Qt.SizeHintRole):
                return QSize(self._spacer_size.width(), self._spacer_size.height())
            if role == Qt.ItemDataRole.DisplayRole:
                return None
            return None

        source_index = self.mapToSource(index)
        if not source_index.isValid():
            return None
        return source.data(source_index, role)

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:  # noqa: N802
        if not index.isValid():
            return Qt.NoItemFlags
        if bool(self.data(index, Roles.IS_SPACER)):
            return Qt.NoItemFlags
        source_index = self.mapToSource(index)
        source = self.sourceModel()
        if source is None or not source_index.isValid():
            return Qt.NoItemFlags
        return source.flags(source_index)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _handle_source_reset(self, *args, **_kwargs) -> None:  # pragma: no cover - Qt signal glue
        self.beginResetModel()
        self.endResetModel()
