from PySide6 import QtCore


class FinishController(QtCore.QObject):
    finish_state_changed = QtCore.Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._armed = False
        self._drivers = []
        self._active_driver = None
        self._active_pattern = None

    def register_driver(self, driver):
        if driver not in self._drivers:
            self._drivers.append(driver)

    def set_armed(self, armed: bool):
        self._armed = armed
        if not armed:
            self.deactivate()

    def is_armed(self) -> bool:
        return self._armed

    def activate(self, pattern) -> bool:
        if not self._armed or pattern is None or self.is_controlling_output():
            return False

        driver = self._resolve_driver(pattern)
        if driver is None or not driver.activate_finish_pattern(pattern):
            return False

        self._active_driver = driver
        self._active_pattern = pattern
        self.finish_state_changed.emit(True)
        return True

    def deactivate(self) -> bool:
        if self._active_driver is None:
            return False

        driver = self._active_driver
        was_active = driver.is_finish_active()
        if not driver.is_finish_controlling_output():
            self._clear_inactive_driver()
            return False

        driver.deactivate_finish()
        changed = was_active and not driver.is_finish_active()
        if changed:
            self.finish_state_changed.emit(False)
        elif not driver.is_finish_controlling_output():
            self._clear_inactive_driver()
        return changed

    def is_active(self) -> bool:
        return self._active_driver is not None and self._active_driver.is_finish_active()

    def is_controlling_output(self) -> bool:
        if self._active_driver is None:
            return False
        if self._active_driver.is_finish_controlling_output():
            return True
        self._clear_inactive_driver()
        return False

    def current_pattern(self):
        if self.is_controlling_output():
            return self._active_pattern
        return None

    def _resolve_driver(self, pattern):
        for driver in self._drivers:
            if driver.can_finish_pattern(pattern):
                return driver
        return None

    def _clear_inactive_driver(self):
        self._active_driver = None
        self._active_pattern = None