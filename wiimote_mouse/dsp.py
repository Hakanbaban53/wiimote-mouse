"""Signal processing: gyro to cursor/scroll deltas, roll estimation, scroll repeat."""

import logging
import math
from collections import deque
from dataclasses import dataclass
from evdev import ecodes as E
from statistics import median

from . import settings

log = logging.getLogger("wiimote-mouse")



X_AXIS = E.ABS_RX


Y_AXIS = E.ABS_RZ


AXES = (X_AXIS, Y_AXIS)


ZERO = (0, 0, 0, 0)


def lowpass_alpha(cutoff_hz, dt):
    tau = 1.0 / (2.0 * math.pi * cutoff_hz)
    return dt / (dt + tau)


class OneEuroFilter:
    """Velocity filter; adaptive smoothing reduces noise without much lag."""

    def __init__(self):
        self.value = None
        self.last_raw = None
        self.derivative = 0.0

    def reset(self):
        self.value = None
        self.last_raw = None
        self.derivative = 0.0

    def step(self, raw, dt):
        if self.value is None:
            self.value = raw
            self.last_raw = raw
            return raw
        rate = (raw - self.last_raw) / dt
        self.last_raw = raw
        derivative_alpha = lowpass_alpha(settings.ONE_EURO_D_CUTOFF, dt)
        self.derivative += derivative_alpha * (rate - self.derivative)
        cutoff = settings.ONE_EURO_MIN_CUTOFF + settings.ONE_EURO_BETA * abs(self.derivative)
        alpha = lowpass_alpha(cutoff, dt)
        self.value += alpha * (raw - self.value)
        return self.value


@dataclass
class Calibration:
    center: dict
    deadzone: dict


def stationary_estimate(samples, now):
    """Calculate median gyro bias and MAD noise for a stationary window.

    The deadzone has no MIN_DEADZONE / MAX_DEADZONE clamps, as requested.
    """
    recent = [s for s in samples if now - s[0] <= settings.CALIBRATION_WINDOW]
    if len(recent) < settings.CALIBRATION_MIN_SAMPLES:
        return None
    if recent[-1][0] - recent[0][0] < 0.75 * settings.CALIBRATION_WINDOW:
        return None

    centers, zones = {}, {}
    for axis, idx in ((X_AXIS, 1), (Y_AXIS, 2)):
        vals = [s[idx] for s in recent]
        if max(vals) - min(vals) > settings.CALIBRATION_MAX_SPREAD:
            return None
        mid = float(median(vals))
        mad = float(median(abs(v - mid) for v in vals))
        centers[axis] = mid
        zones[axis] = settings.NOISE_MULTIPLIER * mad
    return Calibration(centers, zones)


def soft_deadzone(delta, width):
    """Continuously remove the deadzone width; no sharp cursor-speed jump."""
    return math.copysign(max(0.0, abs(delta) - width), delta)


class RollCompensator:
    """Estimates roll (twist around the remote's long axis) from gravity and
    rotates the (yaw, pitch) gyro rates back into a "remote held level" frame,
    so a horizontal sweep stays horizontal when the remote is held on its side.

    Accelerometer axes (hid-wiimote): X, Y, Z in 10-bit counts centered on 0.
    Level remote, buttons up: gravity is on +Z, so roll = atan2(ax, az) = 0.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._x = self._z = None
        self._last = None
        self.valid = False
        self.angle = 0.0

    def update(self, ax, ay, az, now):
        g = settings.ACCEL_UNITS_PER_G
        if abs(math.sqrt(ax * ax + ay * ay + az * az) - g) > settings.ACCEL_GATE * g:
            return      # swinging / shaking: gravity direction unreliable, hold
        if math.hypot(ax, az) < 0.35 * g:
            return      # pointing straight up or down: roll is undefined, hold
        if self._x is None or self._last is None or now - self._last > 0.5:
            self._x, self._z = ax, az
        else:
            dt = max(0.0, now - self._last)
            alpha = dt / (dt + settings.ACCEL_FILTER_TC)
            self._x += alpha * (ax - self._x)
            self._z += alpha * (az - self._z)
        self._last = now
        self.angle = math.atan2(self._x, self._z)
        self.valid = True

    def rotate(self, yaw, pitch):
        theta = -self.angle if settings.ROLL_INVERT else self.angle
        c, s = math.cos(theta), math.sin(theta)
        return yaw * c + pitch * s, pitch * c - yaw * s


class GyroMouse:
    """Turns gyro reports + button state into pointer/scroll deltas.

    `now` passed to frame() is the report's own timestamp (any monotonic-ish
    clock, only differences matter), not the time we got around to reading it.
    """

    def __init__(self):
        self.filters = {axis: OneEuroFilter() for axis in AXES}
        self.fraction = {}
        self.samples = deque(maxlen=120)
        self.b_held = False
        self.a_held = False
        self.roll = RollCompensator()
        self.last_accel = None
        self.on_event = None    # optional callback: "calibration_started" / "calibrated"
        self._reset_gyro()

    def _notify(self, name):
        if self.on_event is not None:
            self.on_event(name)

    @property
    def active(self):
        return self.b_held or self.a_held

    def _reset_gyro(self):
        self.raw = {axis: None for axis in AXES}
        self.center = None
        self.deadzone = {axis: 0.0 for axis in AXES}
        self.samples.clear()
        self.last_check = 0.0
        self.last_bias_update = None
        self.calibrating = True
        self.reset_motion()

    def gyro_lost(self, silent=False):
        """MotionPlus (re)appeared or vanished: forget bias, recalibrate."""
        self._reset_gyro()
        if not silent:
            self._notify("calibration_started")

    def disconnect(self):
        """Whole remote gone: forget held buttons and gyro state."""
        self.b_held = False
        self.a_held = False
        self.roll.reset()
        self.last_accel = None
        self._reset_gyro()

    def feed_accel(self, ax, ay, az, now):
        self.last_accel = (ax, ay, az)
        self.roll.update(ax, ay, az, now)

    def rates_dps(self):
        """Current (yaw, pitch) rotation in deg/s relative to the learned bias,
        or None before calibration. Meant for live display, not for pointing."""
        if self.center is None or any(self.raw[axis] is None for axis in AXES):
            return None
        return tuple(soft_deadzone(self.raw[axis] - self.center[axis], self.deadzone[axis])
                     / settings.GYRO_UNITS_PER_DEGREE_S for axis in AXES)

    def reset_motion(self):
        self.last_frame = None
        self.scroll_fraction = 0.0
        self.legacy_scroll_fraction = 0
        for axis in AXES:
            self.filters[axis].reset()
            self.fraction[axis] = 0.0

    def switch_mode(self, button, pressed):
        if button == "B":
            self.b_held = pressed
        else:
            self.a_held = pressed
        self.reset_motion()
        if not pressed and not self.active:
            # Don't use motion samples for stationary drift detection.
            self.samples.clear()
            self.last_bias_update = None

    def begin_calibration(self):
        self.calibrating = True
        self.samples.clear()
        self.last_bias_update = None
        self.reset_motion()
        log.info("Recalibrating: hold the remote still for about half a second with A and B released...")
        self._notify("calibration_started")

    def frame(self, now):
        """Process a completed gyro report.

        Returns (cursor_dx, cursor_dy, gyro_scroll_hires_delta,
                 gyro_scroll_legacy_delta) or zeros.
        """
        if any(self.raw[axis] is None for axis in AXES):
            return ZERO

        if self.calibrating or not self.active:
            if not self.active:
                self.samples.append((now, self.raw[X_AXIS], self.raw[Y_AXIS]))
                if now - self.last_check >= settings.STILLNESS_CHECK_INTERVAL:
                    self.last_check = now
                    estimate = stationary_estimate(self.samples, now)
                    if estimate is not None:
                        self._apply_estimate(estimate, now)
            self.reset_motion()
            return ZERO

        if self.center is None:
            self.reset_motion()
            return ZERO

        if self.last_frame is None:
            self.last_frame = now
            return ZERO

        dt = now - self.last_frame
        if dt < 0 or dt > settings.MAX_FRAME_GAP:
            self.reset_motion()
            return ZERO
        if dt < settings.MIN_FRAME_TIME:
            return ZERO     # duplicate/too-close report; keep last_frame
        self.last_frame = now
        dt = min(dt, settings.MAX_FRAME_TIME)

        offsets = {axis: self.raw[axis] - self.center[axis] for axis in AXES}
        if any(abs(v) > settings.MAX_ABS_DELTA for v in offsets.values()):
            self.reset_motion()
            return ZERO

        rates = {axis: soft_deadzone(offsets[axis], self.deadzone[axis]) / settings.GYRO_UNITS_PER_DEGREE_S
                 for axis in AXES}
        if settings.ROLL_COMPENSATION and self.roll.valid:
            rates[X_AXIS], rates[Y_AXIS] = self.roll.rotate(rates[X_AXIS], rates[Y_AXIS])
        speed = {}
        for axis in AXES:
            speed[axis] = self.filters[axis].step(rates[axis], dt)
            if abs(speed[axis]) < 0.005:
                speed[axis] = 0.0

        if self.a_held:
            return self._scroll_frame(speed, dt)
        return self._pointer_frame(speed, dt)

    def _apply_estimate(self, estimate, now):
        if self.calibrating or self.center is None:
            self.center = estimate.center
            self.deadzone = estimate.deadzone
            self.calibrating = False
            self.reset_motion()
            log.info("Calibration complete. The mouse is ready.")
            self._notify("calibrated")
        elif all(abs(estimate.center[axis] - self.center[axis]) <= settings.AUTO_BIAS_MAX_CHANGE
                 for axis in AXES):
            # Drift compensation uses only *stationary* windows.
            dt = (now - self.last_bias_update
                  if self.last_bias_update is not None else settings.STILLNESS_CHECK_INTERVAL)
            alpha = 1.0 - math.exp(-max(0.0, dt) / settings.BIAS_UPDATE_TIME_CONSTANT)
            for axis in AXES:
                self.center[axis] += alpha * (estimate.center[axis] - self.center[axis])
                self.deadzone[axis] += alpha * (estimate.deadzone[axis] - self.deadzone[axis])
            self.last_bias_update = now

    def _scroll_frame(self, speed, dt):
        # A turns vertical gyro motion into smooth vertical wheel scrolling.
        # It takes priority over B (so A+B does not also move the pointer).
        vertical = speed[Y_AXIS] * (-1 if settings.INVERT_Y else 1)
        scroll_dir = 1 if settings.INVERT_SCROLL else -1
        self.scroll_fraction += (scroll_dir * vertical *
                                 settings.GYRO_SCROLL_NOTCHES_PER_DEGREE * dt * 120.0)
        highres = math.trunc(self.scroll_fraction)
        self.scroll_fraction -= highres
        self.legacy_scroll_fraction += highres
        legacy = math.trunc(self.legacy_scroll_fraction / 120)
        self.legacy_scroll_fraction -= legacy * 120
        return 0, 0, highres, legacy

    def _pointer_frame(self, speed, dt):
        magnitude = math.hypot(speed[X_AXIS], speed[Y_AXIS])
        gain = settings.SENSITIVITY * (1.0 + settings.POINTER_ACCELERATION *
                                   min(1.0, magnitude / settings.ACCEL_REFERENCE_DPS))
        for axis in AXES:
            self.fraction[axis] += speed[axis] * gain * dt

        dx = math.trunc(self.fraction[X_AXIS])
        dy = math.trunc(self.fraction[Y_AXIS])
        # Clip *before* retaining fractional pixels to prevent a speed backlog.
        dx = max(-settings.MAX_PIXELS_PER_FRAME, min(settings.MAX_PIXELS_PER_FRAME, dx))
        dy = max(-settings.MAX_PIXELS_PER_FRAME, min(settings.MAX_PIXELS_PER_FRAME, dy))
        self.fraction[X_AXIS] -= dx
        self.fraction[Y_AXIS] -= dy
        for axis in AXES:
            if abs(self.fraction[axis]) > 1.0:
                self.fraction[axis] = math.copysign(0.999, self.fraction[axis])

        return (-dx if settings.INVERT_X else dx,
                -dy if settings.INVERT_Y else dy, 0, 0)


# Scroll actions that can auto-repeat while a button is held.
SCROLL_TOKENS = {
    "scroll.up": (E.REL_WHEEL, +1),
    "scroll.down": (E.REL_WHEEL, -1),
    "scroll.left": (E.REL_HWHEEL, -1),
    "scroll.right": (E.REL_HWHEEL, +1),
}


class ScrollRepeater:
    """Tap for one notch; hold for continuous scrolling, with gentle acceleration."""

    def __init__(self):
        self.pressed_at = {}
        self.next_repeat = {}

    def changed(self, token, pressed, now):
        """A scroll button went down/up. Returns the notches to send right now."""
        if token not in SCROLL_TOKENS:
            return []
        if not pressed:
            self.pressed_at.pop(token, None)
            self.next_repeat.pop(token, None)
            return []
        if token in self.pressed_at:
            return []
        self.pressed_at[token] = now
        self.next_repeat[token] = now + settings.DPAD_FIRST_REPEAT_DELAY
        return [SCROLL_TOKENS[token]]

    def release_all(self):
        self.pressed_at.clear()
        self.next_repeat.clear()

    def next_deadline(self):
        """Monotonic time of the next repeat, or None when nothing is held."""
        return min(self.next_repeat.values(), default=None)

    def repeats(self, now):
        result = []
        for token, next_time in list(self.next_repeat.items()):
            if now < next_time:
                continue
            interval = (settings.DPAD_FAST_INTERVAL
                        if now - self.pressed_at[token] >= settings.DPAD_FAST_AFTER
                        else settings.DPAD_REPEAT_INTERVAL)
            # No backlog after lag, just one step at the next opportunity.
            self.next_repeat[token] = now + interval
            result.append(SCROLL_TOKENS[token])
        return result
