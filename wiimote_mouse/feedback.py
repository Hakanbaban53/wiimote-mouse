"""Feedback on the remote itself: LEDs, rumble, battery, desktop notifications."""

import logging
import shutil
import subprocess
import threading
from collections import deque
from evdev import ecodes as E
from evdev import ff

from . import devices
from . import settings

log = logging.getLogger("wiimote-mouse")



def sensitivity_level(value):
    """1..4 bar length for the player LEDs."""
    return 1 + (value >= 12) + (value >= 22) + (value >= 40)


class LedBar:
    """The four player LEDs. They are plain on/off switches in sysfs."""

    def __init__(self, paths):
        self.paths = list(paths)
        self.mask = None
        self.enabled = True

    def set_mask(self, mask):
        if not self.enabled or mask == self.mask:
            return
        for index, path in enumerate(self.paths):
            wanted = bool(mask & (1 << index))
            if self.mask is not None and bool(self.mask & (1 << index)) == wanted:
                continue
            try:
                with open(path, "w", encoding="ascii") as handle:
                    handle.write("1" if wanted else "0")
            except PermissionError:
                log.info("No permission to write the LEDs; LED feedback is off "
                         "(needs the 'wiimote' group and the udev rule, see --doctor).")
                self.enabled = False
                return
            except OSError:
                self.enabled = False    # remote went away
                return
        self.mask = mask


class Rumble:
    """One short on/off buzz (the Wii Remote motor has no strength control)."""

    def __init__(self, dev, duration_ms=90):
        self.dev = dev
        self.effect_id = None
        try:
            if E.FF_RUMBLE not in dev.capabilities().get(E.EV_FF, []):
                return
            effect = ff.Effect(
                E.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(duration_ms, 0),
                ff.EffectType(ff_rumble_effect=ff.Rumble(strong_magnitude=0xC000,
                                                         weak_magnitude=0)))
            self.effect_id = dev.upload_effect(effect)
        except (OSError, AttributeError) as exc:
            log.debug("Rumble unavailable: %s", exc)

    @property
    def available(self):
        return self.effect_id is not None

    def pulse(self):
        if self.effect_id is None:
            return
        try:
            self.dev.write(E.EV_FF, self.effect_id, 1)
        except OSError:
            self.effect_id = None

    def close(self):
        if self.effect_id is not None:
            try:
                self.dev.erase_effect(self.effect_id)
            except OSError:
                pass
            self.effect_id = None


class Notifier:
    """Desktop notifications through notify-send (fire and forget)."""

    def __init__(self, enabled, runner=None):
        self.runner = runner or self._run
        self.enabled = enabled and (runner is not None or shutil.which("notify-send") is not None)

    @staticmethod
    def _run(command):
        try:
            subprocess.run(command, timeout=5, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            pass

    def send(self, title, body, urgent=False):
        if not self.enabled:
            return
        command = ["notify-send", "-a", "Wii Remote Mouse", "-i", "input-gaming",
                   "-u", "critical" if urgent else "normal", title, body]
        threading.Thread(target=self.runner, args=(command,), daemon=True).start()


class BatteryMonitor:
    """Reads the battery in a worker thread: the kernel blocks the reader while
    it waits for the remote's status report, which must never stall the cursor."""

    def __init__(self, read, wake, interval, first_delay):
        self._read = read
        self._wake = wake
        self._interval = interval
        self._first_delay = first_delay
        self._stop = threading.Event()
        self._results = deque()
        self._thread = threading.Thread(target=self._loop, name="battery", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=0.2)

    def poll(self):
        out = []
        while self._results:
            out.append(self._results.popleft())
        return out

    def _loop(self):
        delay = self._first_delay
        while not self._stop.wait(delay):
            delay = self._interval
            try:
                value = self._read()
            except (OSError, ValueError):
                continue
            if self._stop.is_set():
                return
            self._results.append(value)
            self._wake()


class Feedback:
    """Shows state on the remote: LEDs, buzzes, and desktop notifications."""

    LED_MASKS = {"waiting": 0b0001, "asleep": 0b0001, "calibrating": 0b0110}

    def __init__(self, timers, sensitivity, leds=None, rumble=None, notifier=None):
        self.timers = timers
        self.sensitivity = sensitivity
        self.leds = leds
        self.rumble = rumble
        self.notifier = notifier or Notifier(False)
        self.state = None
        self.battery_level = 0      # 0 ok, 1 low (notified), 2 critical (notified)
        self.battery_percent = None
        self.manual_mask = None     # LED test from wiimotectl / the UI; None = automatic
        self.on_change = None       # called after anything a client may want to display
        self._remote = None         # set by create(), used by reconfigure()
        self._calibration_token = 0

    @classmethod
    def create(cls, remote, timers, sensitivity):
        leds = None
        if settings.FEEDBACK_LEDS:
            paths = devices.find_leds(remote.key)
            leds = LedBar(paths) if paths else None
        rumble = Rumble(remote.buttons) if settings.FEEDBACK_RUMBLE else None
        if rumble is not None and not rumble.available:
            rumble = None
        feedback = cls(timers, sensitivity, leds, rumble, Notifier(settings.FEEDBACK_NOTIFY))
        feedback._remote = remote
        return feedback

    def reconfigure(self):
        """Apply changed feedback settings to a running session."""
        remote = self._remote
        if remote is None:
            return
        if settings.FEEDBACK_LEDS and self.leds is None:
            paths = devices.find_leds(remote.key)
            self.leds = LedBar(paths) if paths else None
        elif not settings.FEEDBACK_LEDS and self.leds is not None:
            self.leds.set_mask(0b0001)
            self.leds = None
        if settings.FEEDBACK_RUMBLE and self.rumble is None:
            rumble = Rumble(remote.buttons)
            self.rumble = rumble if rumble.available else None
        elif not settings.FEEDBACK_RUMBLE and self.rumble is not None:
            self.rumble.close()
            self.rumble = None
        self.notifier = Notifier(settings.FEEDBACK_NOTIFY)
        self._refresh()

    # -- states
    def _changed(self):
        if self.on_change is not None:
            self.on_change()

    def _show(self, state):
        self.state = state
        self._refresh()
        self._changed()

    def _refresh(self):
        if self.leds is None or self.state is None:
            return
        if self.manual_mask is not None:
            mask = self.manual_mask
        elif self.state == "ready":
            mask = (1 << sensitivity_level(self.sensitivity)) - 1
        else:
            mask = self.LED_MASKS[self.state]
        self.leds.set_mask(mask)

    def override_leds(self, mask):
        """Show a fixed LED pattern (0-15), or pass None to go back to automatic."""
        self.manual_mask = mask
        self._refresh()
        self._changed()

    def waiting(self):
        self._calibration_token += 1
        self._show("waiting")

    def asleep(self):
        self._calibration_token += 1
        self._show("asleep")

    def calibrating(self):
        self._show("calibrating")
        self._calibration_token += 1
        token = self._calibration_token
        self.timers.after(settings.CALIBRATION_NOTICE_AFTER, lambda: self._calibration_stuck(token))

    def ready(self):
        self._calibration_token += 1
        self._show("ready")
        self.buzz(1)

    def _calibration_stuck(self, token):
        if token != self._calibration_token or self.state != "calibrating":
            return
        log.warning("Calibration is not finishing: put the remote down or hold it still.")
        self.notifier.send("Wii Remote needs to calibrate",
                           "Put the remote down or hold it still for a moment.")
        self.buzz(2)

    def sensitivity_changed(self, value, at_limit=False):
        self.sensitivity = value
        if self.state == "ready":
            self._refresh()
        if at_limit:
            self.buzz(1)
        self._changed()

    def battery(self, percent):
        log.debug("Battery: %.0f%%", percent)
        self.battery_percent = percent
        if percent <= settings.BATTERY_CRITICAL_PERCENT:
            level = 2
        elif percent <= settings.BATTERY_LOW_PERCENT:
            level = 1
        else:
            level = 0
        if level == 0 and percent > settings.BATTERY_LOW_PERCENT + 5:
            self.battery_level = 0          # recovered (fresh batteries); allow warnings again
        if level > self.battery_level:
            self.battery_level = level
            if level == 2:
                log.warning("Wii Remote battery is almost empty (%.0f%%).", percent)
                self.notifier.send("Wii Remote battery almost empty",
                                   f"Battery is at about {percent:.0f}%. Replace the batteries.", urgent=True)
                self.buzz(4)
            else:
                log.warning("Wii Remote battery is low (%.0f%%).", percent)
                self.notifier.send("Wii Remote battery low", f"Battery is at about {percent:.0f}%.")
                self.buzz(3)
        self._changed()

    # -- helpers
    def buzz(self, count):
        if self.rumble is None:
            return
        for index in range(count):
            if index == 0:
                self.rumble.pulse()
            else:
                self.timers.after(index * 0.18, self.rumble.pulse)

    def close(self):
        if self.leds is not None:
            self.leds.set_mask(0b0001)      # back to the kernel's default: LED 1
        if self.rumble is not None:
            self.rumble.close()
