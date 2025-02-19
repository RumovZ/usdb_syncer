"""Model for the filter tree."""

from __future__ import annotations

from typing import Any, cast, overload

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import QWidget

from usdb_syncer.song_data import fuzz_text

from .item import TreeItem

QIndex = QModelIndex | QPersistentModelIndex


class TreeModel(QAbstractItemModel):
    """Model for the filter tree."""

    def __init__(self, parent: QWidget, root: TreeItem) -> None:
        super().__init__(parent)
        self.root = root

    def item_for_index(self, idx: QIndex) -> TreeItem:
        return cast(TreeItem, idx.internalPointer())

    def index_for_item(self, item: TreeItem) -> QModelIndex:
        return self.createIndex(item.row_in_parent, 0, item)

    ### change data

    def set_checked(self, item: TreeItem, checked: bool) -> None:
        if checked and item.parent:
            for sibling in item.parent.children:
                sibling.checked = False

        item.checked = checked

    ### QAbstractItemModel implementation

    def rowCount(self, parent: QIndex = QModelIndex()) -> int:
        if not parent.isValid():
            return len(self.root.children)
        item = cast(TreeItem, parent.internalPointer())
        return len(item.children)

    def columnCount(self, _parent: QIndex = QModelIndex()) -> int:
        return 1

    def index(
        self, row: int, column: int, parent: QIndex = QModelIndex()
    ) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            parent_item = self.root
        else:
            parent_item = cast(TreeItem, parent.internalPointer())
        item = parent_item.children[row]
        return self.createIndex(row, column, item)

    @overload
    def parent(self) -> QObject: ...

    @overload
    def parent(self, child: QIndex) -> QModelIndex: ...

    def parent(self, child: QIndex | None = None) -> QModelIndex | QObject:
        if child is None:
            return super().parent()
        if not child.isValid():
            return QModelIndex()
        child_item = cast(TreeItem, child.internalPointer())
        parent_item = child_item.parent
        if parent_item is None or parent_item is self.root:
            return QModelIndex()
        return self.createIndex(parent_item.row_in_parent, 0, parent_item)

    def data(self, index: QIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            return str(self.item_for_index(index).data)
        if role == Qt.ItemDataRole.CheckStateRole:
            return self.item_for_index(index).checked
        if role == Qt.ItemDataRole.DecorationRole:
            return self.item_for_index(index).decoration()
        return None

    def flags(self, index: QIndex) -> Qt.ItemFlag:
        flags = Qt.ItemFlag.ItemIsEnabled
        item = self.item_for_index(index)
        if item.checkable:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags


class TreeProxyModel(QSortFilterProxyModel):
    """Proxy model for filtering the filter tree by text."""

    def __init__(self, parent: QObject, source_model: TreeModel) -> None:
        super().__init__(parent)
        self._source = source_model
        self.setSourceModel(source_model)
        self._filter: list[str] = []

        self._filter_invalidation_timer = QTimer(parent)
        self._filter_invalidation_timer.setSingleShot(True)
        self._filter_invalidation_timer.setInterval(600)
        self._filter_invalidation_timer.timeout.connect(self.invalidateRowsFilter)

    def filterAcceptsRow(self, source_row: int, source_parent: QIndex) -> bool:
        if not self._filter or not source_parent.isValid():
            return True
        parent = self._source.item_for_index(source_parent)
        item = parent.children[source_row]
        return item.filter_accepts_row(self._filter)

    def set_filter(self, text: str) -> None:
        self._filter = fuzz_text(text).split()
        self._filter_invalidation_timer.start()
