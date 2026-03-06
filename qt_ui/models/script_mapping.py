from __future__ import annotations  # multiple return values

import json.decoder
import typing
import logging

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QColor

import qt_ui.device_wizard
import qt_ui.device_wizard.axes
from qt_ui.device_wizard.axes import AxisEnum
from qt_ui.models.tree_item import TreeItem
from qt_ui.models.funscript_kit import FunscriptKitModel
from funscript.funscript import Funscript
import funscript.collect_funscripts
from funscript.collect_funscripts import collect_funscripts_grouped

logger = logging.getLogger('restim.script_mapping')


class HeaderTreeItem(TreeItem):
    def __init__(self, parent: TreeItem=None):
        super(HeaderTreeItem, self).__init__(parent)

    def data(self, column):
        items = ['resource', 'target', 'actions']
        return items[column]


class ResourceCategory(TreeItem):
    def __init__(self, name: str, parent: TreeItem=None):
        super(ResourceCategory, self).__init__(parent)

        self.name = name

    def data(self, column):
        if column == 0:
            return self.name
        return None


class FunscriptTreeItem(TreeItem):
    def __init__(self, resource: funscript.collect_funscripts.Resource, parent: TreeItem=None):
        super(FunscriptTreeItem, self).__init__(parent)

        # self.resource = resource
        self.file_name = resource.name()
        self.file_path = str(resource.path)
        self.funscript_type = resource.funscript_type()
        self.axis = qt_ui.device_wizard.axes.AxisEnum.NONE
        self.is_removable = False
        self.first_of_its_kind = False
        self.enabled = True

        # load funscript immediately. This makes the UI feel sluggish on connecting/disconnecting
        # but has the advantage of not requiring any file IO when audio starts.
        try:
            self.script = Funscript.from_file(resource.path)
        except json.decoder.JSONDecodeError as e:
            logger.error(f'Unable to parse funscript, broken? {resource.path}')
            self.script = None

    def has_broken_script(self) -> bool:
        return self.script is None

    def data(self, column):
        if column == 0:
            return self.file_name
        if column == 1:
            if self.has_broken_script():
                return "funscript loading failed"
            return self.axis.display_name()
        if column == 2:
            return self.is_removable

    def edit_data(self, column):
        if column == 0:
            return self.file_name
        if column == 1:
            return self.axis
        if column == 2:
            return self.is_removable


class TCodeTreeItem(TreeItem):
    def __init__(self, axis: str, parent: TreeItem = None):
        super(TCodeTreeItem, self).__init__(parent)

        self.axis = axis

    def data(self, column):
        if column == 0:
            return self.axis
        return 'todo'


class ScriptMappingModel(QAbstractItemModel):
    def __init__(self):
        super(ScriptMappingModel, self).__init__()

        self._root = HeaderTreeItem()
        self._funscripts_manual = ResourceCategory('Funscripts (manual)', self._root)
        self._root.appendChild(self._funscripts_manual)
        self._funscripts_auto_categories: list[ResourceCategory] = []

    def data(self, index: QModelIndex, role: int = ...) -> typing.Any:
        if not index.isValid():
            return None

        item = index.internalPointer()

        if role == Qt.CheckStateRole and index.column() == 0:
            if isinstance(item, FunscriptTreeItem):
                return Qt.Checked if item.enabled else Qt.Unchecked
            elif isinstance(item, ResourceCategory) and item is not self._funscripts_manual:
                states = {child.enabled for child in item.children}
                if states == {True}:
                    return Qt.Checked
                elif states == {False}:
                    return Qt.Unchecked
                else:
                    return Qt.PartiallyChecked
            return None

        if role == Qt.ToolTipRole:
            if isinstance(item, FunscriptTreeItem):
                return item.file_path
            return None
        if role == Qt.EditRole:
            return item.edit_data(index.column())
        elif role == Qt.DisplayRole:
            return item.data(index.column())
        elif role == Qt.ForegroundRole:
            if isinstance(item, FunscriptTreeItem):
                if item.has_broken_script():
                    return QColor(255, 0, 0)
                if not item.enabled:
                    return QColor(128, 128, 128)
            return None
        else:
            return None

    def setData(self, index: QModelIndex, value: typing.Any, role: int = ...) -> bool:
        # print('setData', index.row(), index.column(), value)
        if role == Qt.CheckStateRole and index.column() == 0:
            item = index.internalPointer()
            new_enabled = value == Qt.Checked.value
            if isinstance(item, FunscriptTreeItem):
                if item.enabled != new_enabled:
                    item.enabled = new_enabled
                    self.refresh_active_files()
                    self.dataChanged.emit(index, index, [role])
                    # update parent category tri-state
                    parent_idx = self.parent(index)
                    if parent_idx.isValid():
                        self.dataChanged.emit(parent_idx, parent_idx, [Qt.CheckStateRole])
                    return True
            elif isinstance(item, ResourceCategory) and item is not self._funscripts_manual:
                parent_idx = index
                for row in range(item.childCount()):
                    child = item.child(row)
                    if isinstance(child, FunscriptTreeItem):
                        child.enabled = new_enabled
                        child_idx = self.index(row, 0, parent_idx)
                        self.dataChanged.emit(child_idx, child_idx, [Qt.CheckStateRole])
                self.refresh_active_files()
                self.dataChanged.emit(index, index, [role])
                return True
            return False

        if role == Qt.EditRole:
            if isinstance(index.internalPointer(), FunscriptTreeItem):
                if index.column() == 1:
                    if index.internalPointer().axis != value:
                        # only emit signals if data actually changed
                        index.internalPointer().axis = value
                        self.refresh_active_files()
                        self.dataChanged.emit(index, index, [role])
                        return True
        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags

        item = index.internalPointer()

        if index.column() == 0:
            if isinstance(item, FunscriptTreeItem):
                base = Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
                if not item.first_of_its_kind:
                    base &= ~Qt.ItemIsEnabled
                return base
            elif isinstance(item, ResourceCategory) and item is not self._funscripts_manual:
                return Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate

            return Qt.ItemIsEnabled
        elif index.column() == 1:
            if isinstance(index.internalPointer(), ResourceCategory):
                return Qt.ItemIsEnabled
            if isinstance(index.internalPointer(), FunscriptTreeItem):
                if index.internalPointer().has_broken_script():
                    return Qt.NoItemFlags
                else:
                    return Qt.ItemIsEnabled | Qt.ItemIsEditable
            return Qt.ItemIsEnabled | Qt.ItemIsEditable
        elif index.column() == 2:
            return Qt.ItemIsEnabled
        # return super(TreeModel, self).flags(index)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = ...) -> typing.Any:
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self._root.data(section)
        return None

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            parentItem = self._root
        else:
            parentItem = parent.internalPointer()

        childItem = parentItem.child(row)
        if childItem:
            return self.createIndex(row, column, childItem)
        return QModelIndex()

    def parent(self,  index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()

        childItem = index.internalPointer()
        parentItem = childItem.parentItem()

        if parentItem == self._root:
            return QModelIndex()

        return self.createIndex(parentItem.row(), 0, parentItem)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0

        if not parent.isValid():
            parentItem = self._root
        else:
            parentItem = parent.internalPointer()

        return parentItem.childCount()

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return parent.internalPointer().columnCount()
        return self._root.columnCount()

    def removeRow(self, row: int, parent: QModelIndex = ...) -> bool:
        # print(parent.internalPointer(), row)
        self.beginRemoveRows(parent, row, row)
        parent.internalPointer().removeChild(row)
        self.endRemoveRows()
        self.dataChanged.emit(parent, parent, [Qt.EditRole])
        self.refresh_active_files()
        return True

    def _all_auto_children(self) -> list[FunscriptTreeItem]:
        result = []
        for cat in self._funscripts_auto_categories:
            result.extend(cat.children)
        return result

    def add_funscript_resource_manual(self, item: FunscriptTreeItem):
        item.parent = self._funscripts_manual
        item.is_removable = True
        self._funscripts_manual.appendChild(item)
        self.refresh_active_files()

    def funscript_conifg(self) -> list[FunscriptTreeItem]:
        return self._funscripts_manual.children + self._all_auto_children()

    def _enabled_items(self) -> list[FunscriptTreeItem]:
        return [item for item in self.funscript_conifg() if item.enabled]

    def get_config_for_axis(self, axis: AxisEnum) -> FunscriptTreeItem | None:
        for funscript in self._enabled_items():
            if funscript.axis == axis:
                return funscript
        return None

    def refresh_active_files(self):
        used = {AxisEnum.NONE}
        for item in self._funscripts_manual.children + self._all_auto_children():
            use = item.enabled and item.axis not in used
            if item.enabled:
                used.add(item.axis)

            if use != item.first_of_its_kind:
                item.first_of_its_kind = use
                index = self.createIndex(item.row(), 0, item.parent)
                self.dataChanged.emit(index, index, [Qt.DisplayRole])

    def detect_funscripts_from_path(self, search_directories: [str], media_file: str) -> bool:
        """
        :param search_directories: list of strings
        :param media_file: "video.mp4"
        :return:
        """
        dirty = len(self._funscripts_auto_categories) > 0
        self._clear_auto_categories()

        grouped = collect_funscripts_grouped(search_directories, media_file)
        is_first_batch = True
        for dir_name, resources in grouped:
            if not resources:
                continue
            dirty |= True
            category = ResourceCategory(dir_name, self._root)
            self._root.appendChild(category)
            self._funscripts_auto_categories.append(category)
            for res in resources:
                item = FunscriptTreeItem(res)
                item.parent = category
                item.enabled = is_first_batch
                category.appendChild(item)
            is_first_batch = False
        return dirty

    def _clear_auto_categories(self):
        for cat in self._funscripts_auto_categories:
            idx = self._root.children.index(cat)
            del self._root.children[idx]
        self._funscripts_auto_categories.clear()

    def clear_auto_detected_funscripts(self):
        if self._funscripts_auto_categories:
            self._clear_auto_categories()
            return True
        return False

    def auto_link_funscripts(self, kit: FunscriptKitModel) -> None:
        for item in self.funscript_conifg():
            self.auto_link_funscript(kit, item)
        self.refresh_active_files()

    def all_auto_categories(self) -> list[ResourceCategory]:
        return self._funscripts_auto_categories

    def auto_link_funscript(self, kit: FunscriptKitModel, item: FunscriptTreeItem):
        if item.has_broken_script():
            return

        suffix = item.funscript_type
        if suffix:
            for kit_item in kit.funscript_conifg():
                if kit_item.auto_loading and kit_item.allow_funscript_control:
                    if suffix in kit_item.funscript_names:
                        item.axis = kit_item.axis
                        logger.info(f'auto-linking `{item.file_name}` to {kit_item.axis.display_name()}.')
                        return
        logger.info(f'auto-linking `{item.file_name}` failed')
