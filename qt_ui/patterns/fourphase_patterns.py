import time
import logging
import numpy as np
from PySide6 import QtCore
import qt_ui.settings
from stim_math.axis import AbstractAxis, is_script_axis
from qt_ui.patterns.fourphase.mouse import MousePattern
from qt_ui.patterns.fourphase.sequence import SequencePattern

logger = logging.getLogger('restim.motion_generation')

class POINTS:
    A = np.array([1, .33, .33, .33])
    B = np.array([.33, 1, .33, .33])
    C = np.array([.33, .33, 1, .33])
    D = np.array([.33, .33, .33, 1])

    AB = np.array([1, 1, 0, 0])
    AC = np.array([1, 0, 1, 0])
    AD = np.array([1, 0, 0, 1])
    BC = np.array([0, 1, 1, 0])
    BD = np.array([0, 1, 0, 1])
    CD = np.array([0, 0, 1, 1])

    ABC = np.array([1, 1, 1, 0])
    ABD = np.array([1, 1, 0, 1])
    ACD = np.array([1, 0, 1, 1])
    BCD = np.array([0, 1, 1, 1])


    CENTER = np.array([1, 1, 1, 1])


class FourphaseMotionGenerator(QtCore.QObject):
    def __init__(self, parent,
                 intensity_a: AbstractAxis,
                 intensity_b: AbstractAxis,
                 intensity_c: AbstractAxis,
                 intensity_d: AbstractAxis):
        super().__init__(parent)

        self.intensity_a = intensity_a
        self.intensity_b = intensity_b
        self.intensity_c = intensity_c
        self.intensity_d = intensity_d

        self.script_a = None
        self.script_b = None
        self.script_c = None
        self.script_d = None

        # Instantiate MousePattern with axes
        self.mouse_pattern = MousePattern(intensity_a, intensity_b, intensity_c, intensity_d)

        # Create patterns like the original implementation
        self.patterns = [
            self.mouse_pattern,
            SequencePattern('seq A→B→C→D', [POINTS.A, POINTS.B, POINTS.C, POINTS.D]),
            SequencePattern('seq A→B→D→C', [POINTS.A, POINTS.B, POINTS.D, POINTS.C]),
            SequencePattern('seq A→C→B→D', [POINTS.A, POINTS.C, POINTS.B, POINTS.D]),
            SequencePattern('seq A→C→D→B', [POINTS.A, POINTS.C, POINTS.D, POINTS.B]),
            SequencePattern('seq A→D→B→C', [POINTS.A, POINTS.D, POINTS.B, POINTS.C]),
            SequencePattern('seq A→D→C→B', [POINTS.A, POINTS.D, POINTS.C, POINTS.B]),

            SequencePattern('seq A→B→C→D (slow)', [POINTS.A, POINTS.AB, POINTS.B, POINTS.BC, POINTS.C, POINTS.CD, POINTS.D, POINTS.AD]),

            SequencePattern('seq AB→BC→CD→AD', [POINTS.AB, POINTS.BC, POINTS.CD, POINTS.AD]),
            SequencePattern('seq AB→BD→CD→AC', [POINTS.AB, POINTS.BD, POINTS.CD, POINTS.AC]),

            SequencePattern('A <-> B', [POINTS.A, POINTS.AB, POINTS.B, POINTS.AB]),
            SequencePattern('A <-> C', [POINTS.A, POINTS.AC, POINTS.C, POINTS.AC]),
            SequencePattern('A <-> D', [POINTS.A, POINTS.AD, POINTS.D, POINTS.AD]),
            SequencePattern('B <-> C', [POINTS.B, POINTS.BC, POINTS.C, POINTS.BC]),

            SequencePattern('A <-> center', [POINTS.A, POINTS.CENTER]),
            SequencePattern('B <-> center', [POINTS.B, POINTS.CENTER]),
            SequencePattern('C <-> center', [POINTS.C, POINTS.CENTER]),
            SequencePattern('D <-> center', [POINTS.D, POINTS.CENTER]),

            SequencePattern('A+D <-> center', [POINTS.AD, POINTS.CENTER]),
            SequencePattern('B+C <-> center', [POINTS.BC, POINTS.CENTER]),

            SequencePattern('A + [B→C→D]', [POINTS.AB, POINTS.AC, POINTS.AD]),
            SequencePattern('A + [B→C→D] (slow)', [POINTS.AB, POINTS.ABC, POINTS.AC, POINTS.ACD, POINTS.AD, POINTS.ABD]),
            SequencePattern('A + [B→C→D] (center)', [POINTS.AB, POINTS.CENTER, POINTS.AC, POINTS.CENTER, POINTS.AD, POINTS.CENTER]),
        ]
        self.pattern = self.mouse_pattern

        self._finish_active = False
        self._finish_pattern = None
        self._finish_ramp_duration = 0.3
        self._finish_ramp_start = None
        self._finish_ramping_in = False
        self._finish_ramp_out = False

        self.velocity = 1
        self.last_update_time = time.time()
        self.latency = 0

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.timeout)
        self.timer.setInterval(int(1000 / 60.0))
        self.refreshSettings()

    def set_enable(self, enable):
        if enable:
            self.timer.start()
        else:
            self.timer.stop()

    def set_pattern(self, pattern):
        if isinstance(pattern, MousePattern):
            self.pattern = self.mouse_pattern
        elif pattern in self.patterns:
            self.pattern = pattern
        else:
            # Unknown pattern (e.g. threephase/YAML) — fall back to mouse
            # so motion_3's alpha/beta→4phase bridge drives the electrodes
            self.pattern = self.mouse_pattern

    def set_scripts(self, intensity_a, intensity_b, intensity_c, intensity_d):
        self.script_a = intensity_a if is_script_axis(intensity_a) else None
        self.script_b = intensity_b if is_script_axis(intensity_b) else None
        self.script_c = intensity_c if is_script_axis(intensity_c) else None
        self.script_d = intensity_d if is_script_axis(intensity_d) else None

    def any_scripts_loaded(self):
        return (self.script_a, self.script_b, self.script_c, self.script_d) != (None, None, None, None)

    def set_velocity(self, velocity):
        self.velocity = velocity

    def can_finish_pattern(self, pattern) -> bool:
        return pattern is self.mouse_pattern or pattern in self.patterns

    def activate_finish_pattern(self, pattern) -> bool:
        if not self.can_finish_pattern(pattern):
            return False
        self._finish_pattern = self.mouse_pattern if pattern is self.mouse_pattern else pattern
        self._finish_active = True
        self._finish_ramping_in = True
        self._finish_ramp_start = time.time()
        self._finish_ramp_out = False
        self.finish_state_changed.emit(True)
        logger.info(f"Finish activated: {self._finish_pattern.name()}")
        return True

    def deactivate_finish(self) -> bool:
        if self._finish_active:
            self._finish_active = False
            self._finish_ramping_in = False
            self._finish_ramp_start = time.time()
            self._finish_ramp_out = True
            self.finish_state_changed.emit(False)
            logger.info("Finish deactivating (ramping out)")
            return True
        return False

    def is_finish_active(self) -> bool:
        return self._finish_active

    def is_finish_controlling_output(self) -> bool:
        return self._finish_active or self._finish_pattern is not None or self._finish_ramp_start is not None

    def timeout(self):
        dt = time.time() - self.last_update_time
        self.last_update_time = time.time()
        lagged_time = time.time() - self.latency

        finish_blend = 0.0
        if self._finish_ramp_start is not None:
            elapsed_ramp = time.time() - self._finish_ramp_start
            t = min(1.0, elapsed_ramp / self._finish_ramp_duration) if self._finish_ramp_duration > 0 else 1.0
            if self._finish_ramping_in:
                finish_blend = t
                if t >= 1.0:
                    self._finish_ramp_start = None
            else:
                finish_blend = 1.0 - t
                if t >= 1.0:
                    self._finish_ramp_start = None
                    self._finish_ramp_out = False
                    self._finish_pattern = None
        elif self._finish_active:
            finish_blend = 1.0

        is_blending = self._finish_ramp_start is not None

        if not self.any_scripts_loaded() or (self._finish_active and finish_blend >= 1.0 and not is_blending):
            active_pattern = self._finish_pattern if self._finish_active else self.pattern
            if isinstance(active_pattern, MousePattern):
                if self.pattern.last_position_is_mouse_position():
                    a = self.intensity_a.last_value()
                    b = self.intensity_b.last_value()
                    c = self.intensity_c.last_value()
                    d = self.intensity_d.last_value()
                else:
                    a = self.intensity_a.interpolate(lagged_time)
                    b = self.intensity_b.interpolate(lagged_time)
                    c = self.intensity_c.interpolate(lagged_time)
                    d = self.intensity_d.interpolate(lagged_time)
                self.position_updated.emit(a, b, c, d)
            else:
                a, b, c, d = active_pattern.update(dt * self.velocity)
                self.intensity_a.add(a)
                self.intensity_b.add(b)
                self.intensity_c.add(c)
                self.intensity_d.add(d)
                a = self.intensity_a.interpolate(lagged_time)
                b = self.intensity_b.interpolate(lagged_time)
                c = self.intensity_c.interpolate(lagged_time)
                d = self.intensity_d.interpolate(lagged_time)
                self.position_updated.emit(a, b, c, d)
        elif is_blending and self._finish_pattern is not None:
            fs_a = self.script_a.interpolate(lagged_time) if self.script_a else self.intensity_a.interpolate(lagged_time)
            fs_b = self.script_b.interpolate(lagged_time) if self.script_b else self.intensity_b.interpolate(lagged_time)
            fs_c = self.script_c.interpolate(lagged_time) if self.script_c else self.intensity_c.interpolate(lagged_time)
            fs_d = self.script_d.interpolate(lagged_time) if self.script_d else self.intensity_d.interpolate(lagged_time)

            if isinstance(self._finish_pattern, MousePattern):
                pa = self.intensity_a.interpolate(lagged_time)
                pb = self.intensity_b.interpolate(lagged_time)
                pc = self.intensity_c.interpolate(lagged_time)
                pd = self.intensity_d.interpolate(lagged_time)
            else:
                pa, pb, pc, pd = self._finish_pattern.update(dt * self.velocity)

            a = fs_a * (1.0 - finish_blend) + pa * finish_blend
            b = fs_b * (1.0 - finish_blend) + pb * finish_blend
            c = fs_c * (1.0 - finish_blend) + pc * finish_blend
            d = fs_d * (1.0 - finish_blend) + pd * finish_blend
            self.intensity_a.add(a)
            self.intensity_b.add(b)
            self.intensity_c.add(c)
            self.intensity_d.add(d)
            self.position_updated.emit(a, b, c, d)
        else:
            if self.script_a:
                a = self.script_a.interpolate(lagged_time)
            else:
                a = self.intensity_a.interpolate(lagged_time)
            if self.script_b:
                b = self.script_b.interpolate(lagged_time)
            else:
                b = self.intensity_b.interpolate(lagged_time)
            if self.script_c:
                c = self.script_c.interpolate(lagged_time)
            else:
                c = self.intensity_c.interpolate(lagged_time)
            if self.script_d:
                d = self.script_d.interpolate(lagged_time)
            else:
                d = self.intensity_d.interpolate(lagged_time)
            self.position_updated.emit(a, b, c, d)

    def mouse_event(self, a, b, c, d):
        # add the coordinates from the mouse event to the axes
        if self.pattern == self.mouse_pattern and not self.any_scripts_loaded():
            self.mouse_pattern.mouse_event(a, b, c, d)
            self.intensity_a.add(a)
            self.intensity_b.add(b)
            self.intensity_c.add(c)
            self.intensity_d.add(d)
            self.position_updated.emit(a, b, c, d)  # ???

    def refreshSettings(self):
        self.timer.setInterval(int(1000 // np.clip(qt_ui.settings.display_fps.get(), 1.0, 500.0)))
        self.latency = qt_ui.settings.display_latency.get() / 1000.0

    # triggers display update
    position_updated = QtCore.Signal(float, float, float, float)  # a, b, c
    finish_state_changed = QtCore.Signal(bool)  # active/inactive
