"""Shared fakes for the tests: fake evdev devices, a scriptable /dev/input, recorders."""

import errno
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from evdev import ecodes as E

from wiimote_mouse import dsp, settings


def ev(type_, code, value=0, ts=0.0):
    return SimpleNamespace(type=type_, code=code, value=value, timestamp=lambda: ts)


def key(code, value):
    return ev(E.EV_KEY, code, value)


def stationary_report(t, bias=(100.0, -50.0), wobble=0):
    return [ev(E.EV_ABS, dsp.X_AXIS, bias[0] + wobble, t),
            ev(E.EV_ABS, dsp.Y_AXIS, bias[1], t),
            ev(E.EV_SYN, E.SYN_REPORT, 0, t)]


def feed(controller, reports):
    """Feed (t, x, y) reports straight into GyroMouse; return the summed output."""
    total = [0, 0, 0, 0]
    for t, x, y in reports:
        controller.raw[dsp.X_AXIS] = x
        controller.raw[dsp.Y_AXIS] = y
        for i, v in enumerate(controller.frame(t)):
            total[i] += v
    return total


def calibrated_controller(t0=1000.0, bias=(100.0, -50.0)):
    c = dsp.GyroMouse()
    feed(c, [(t0 + i * 0.01, bias[0] + (i % 3), bias[1] + (i % 2)) for i in range(100)])
    assert not c.calibrating
    return c


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


_LAYER_STATE = ("_config_values", "_overrides", "_ui_profiles", "_raw_config", "_overrides_path", "_dirty")


class SettingsIsolation(unittest.TestCase):
    """Tests rewrite module settings and layer state: put everything back afterwards."""

    def setUp(self):
        saved = {n: v for n, v in vars(settings).items()
                 if n.isupper() and isinstance(v, (bool, int, float))}
        layers = {n: (v.copy() if isinstance(v, dict) else v)
                  for n, v in ((n, getattr(settings, n)) for n in _LAYER_STATE)}

        def restore():
            for name, value in saved.items():
                setattr(settings, name, value)
            for name, value in layers.items():
                setattr(settings, name, value)

        self.addCleanup(restore)


class FakeDevice:
    def __init__(self):
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)
        self.queue = []
        self.dead = False
        self.grabbed = False
        self.closed = False
        self.grab_error = None
        self.active = set()
        self.open_count = 0
        self.ff_plays = []
        self.erased = []

    def capabilities(self, verbose=False, absinfo=True):
        return {E.EV_FF: [E.FF_RUMBLE]}

    def upload_effect(self, effect):
        return 7

    def erase_effect(self, effect_id):
        self.erased.append(effect_id)

    def write(self, type_, code, value):
        self.ff_plays.append((type_, code, value))

    def active_keys(self):
        return sorted(self.active)

    def fileno(self):
        return self.r

    def push(self, *events):
        self.queue.extend(events)
        os.write(self.w, b"x")

    def kill(self):
        self.dead = True
        os.write(self.w, b"x")

    def read(self):
        try:
            os.read(self.r, 4096)
        except BlockingIOError:
            pass
        if self.dead:
            raise OSError(errno.ENODEV, "No such device")
        events, self.queue = self.queue, []
        yield from events

    def grab(self):
        if self.grab_error:
            raise self.grab_error
        self.grabbed = True

    def ungrab(self):
        if self.dead:
            raise OSError(errno.ENODEV, "No such device")
        self.grabbed = False

    def close(self):
        self.closed = True


class RecordingMouse:
    def __init__(self):
        self.log = []
        self._down = set()

    def close(self):
        pass

    def button(self, code, down):
        if down == (code in self._down):
            return      # like VirtualMouse: no event when nothing changes
        (self._down.add if down else self._down.discard)(code)
        self.log.append(("button", code, down))

    def release_all(self):
        self._down.clear()
        self.log.append(("release_all",))

    def emit(self, dx=0, dy=0, hires=0, steps=0):
        if dx or dy or hires or steps:
            self.log.append(("emit", dx, dy, hires, steps))

    def scroll(self, wheel, steps, hires=0):
        self.log.append(("scroll", wheel, steps, hires))

    def count(self, kind):
        return sum(1 for e in self.log if e[0] == kind)


class RecordingKeyboard:
    def __init__(self):
        self.log = []
        self.held = set()

    def press(self, codes):
        self.held |= set(codes)
        self.log.append(("press", tuple(codes)))

    def release(self, codes):
        self.held -= set(codes)
        self.log.append(("release", tuple(codes)))

    def tap(self, codes):
        self.log.append(("tap", tuple(codes)))

    def release_all(self):
        self.held.clear()
        self.log.append(("release_all",))

    def close(self):
        pass


class RecordingRunner:
    """Stands in for CommandRunner: remembers commands instead of running them."""

    def __init__(self):
        self.commands = []

    def submit(self, argv):
        self.commands.append(list(argv))


class World:
    """Scriptable stand-in for /dev/input: paths -> fake devices."""

    def __init__(self):
        self.devices = {}
        self.remotes = {}

    def add_remote(self, key_, buttons_path, gyro_path=None, accel_path=None, **extensions):
        """Returns (buttons, gyro, accel); extension devices are in ``self.devices``."""
        buttons = self.devices[buttons_path] = FakeDevice()
        entry = {"buttons": buttons_path}
        gyro = accel = None
        if gyro_path:
            gyro = self.devices[gyro_path] = FakeDevice()
            entry["gyro"] = gyro_path
        if accel_path:
            accel = self.devices[accel_path] = FakeDevice()
            entry["accel"] = accel_path
        for slot, path in extensions.items():
            self.devices[path] = FakeDevice()
            entry[slot] = path
        self.remotes[key_] = entry
        return buttons, gyro, accel

    def add_gyro(self, key_, gyro_path):
        gyro = self.devices[gyro_path] = FakeDevice()
        self.remotes[key_]["gyro"] = gyro_path
        return gyro

    def add_extension(self, key_, slot, path):
        device = self.devices[path] = FakeDevice()
        self.remotes[key_][slot] = path
        return device

    def remove_extension(self, key_, slot):
        self.remotes[key_].pop(slot, None)

    def remove_remote(self, key_):
        self.remotes.pop(key_, None)

    def scan(self):
        return {k: dict(v) for k, v in self.remotes.items()}

    def open(self, path):
        device = self.devices[path]
        device.open_count += 1
        device.closed = False
        return device


class FakeSysfs:
    """A sysfs tree like the real one: class nodes are symlinks to
    .../<hid device>/<class>/<node>, which is how hid-wiimote nodes are laid out."""

    def __init__(self, root):
        self.root = root

    def _node(self, hid, cls, name, files):
        real = os.path.join(self.root, "devices", hid, cls, name)
        os.makedirs(real)
        for fname, content in files.items():
            with open(os.path.join(real, fname), "w") as fh:
                fh.write(content)
        link_dir = os.path.join(self.root, "class", cls)
        os.makedirs(link_dir, exist_ok=True)
        return real, link_dir

    def input(self, hid, number, event, name):
        real, _ = self._node(hid, "input", f"input{number}", {"name": name + "\n"})
        ev_dir = os.path.join(self.root, "class", "input", f"event{event}")
        os.makedirs(ev_dir)
        os.symlink(real, os.path.join(ev_dir, "device"))
        return f"/dev/input/event{event}"

    def led(self, hid, index):
        name = f"{hid}:blue:p{index}"
        real, link_dir = self._node(hid, "leds", name, {"brightness": "0\n"})
        os.symlink(real, os.path.join(link_dir, name))
        return os.path.join(real, "brightness")

    def battery(self, hid, address, capacity="78"):
        name = f"wiimote_battery_{address}"
        real, link_dir = self._node(hid, "power_supply", name, {"capacity": capacity + "\n"})
        os.symlink(real, os.path.join(link_dir, name))
        return os.path.join(real, "capacity")

    def patch(self, nodes=()):
        from wiimote_mouse import devices
        return mock.patch.multiple(
            devices, SYSFS_INPUT=os.path.join(self.root, "class", "input"),
            SYSFS_LEDS=os.path.join(self.root, "class", "leds"),
            SYSFS_POWER=os.path.join(self.root, "class", "power_supply"),
            list_devices=lambda: list(nodes))


def temp_dir(test):
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    return directory.name


class FakeLeds:
    def __init__(self):
        self.masks = []

    @property
    def mask(self):
        return self.masks[-1] if self.masks else None

    def set_mask(self, mask):
        if not self.masks or self.masks[-1] != mask:
            self.masks.append(mask)


class FakeRumble:
    def __init__(self):
        self.pulses = 0
        self.closed = False

    def pulse(self):
        self.pulses += 1

    def close(self):
        self.closed = True


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, body, urgent=False):
        self.sent.append((title, urgent))


class DaemonCase(SettingsIsolation):
    """A Daemon wired to fake devices, a recording mouse/keyboard and (optionally) a real
    control socket, running on its own thread like in production."""

    serve = False           # subclasses set True to start the control socket

    def setUp(self):
        import select
        import threading
        from pathlib import Path

        from wiimote_mouse import daemon, devices, runtime
        super().setUp()
        self.dir = Path(temp_dir(self))
        self.config_path = self.dir / "config.toml"
        self.overrides_path = self.dir / "overrides.json"
        self.state_path = self.dir / "state.json"
        self.socket_path = self.dir / "control.sock"
        self.world = World()
        self.mouse = RecordingMouse()
        self.keyboard = RecordingKeyboard()
        self.keyboards_made = 0
        self.runner = RecordingRunner()
        self.shutdown = runtime.Shutdown()
        self.state = runtime.StateStore(self.state_path)
        self.select_timeouts = []
        real_select = select.select

        def spy_select(r, w, x, timeout=None):
            self.select_timeouts.append(timeout)
            return real_select(r, w, x, timeout)

        def make_keyboard():
            self.keyboards_made += 1
            return self.keyboard

        self.daemon = daemon.Daemon(self.mouse, self.shutdown, self.state,
                                    keyboard_factory=make_keyboard, commands=self.runner)
        self.daemon.config_path = self.config_path
        self.daemon.overrides_path = self.overrides_path
        self.controller = self.daemon.controller
        settings.FEEDBACK_NOTIFY = False
        self.configure()
        for target, name, value in (
                (devices, "scan_remotes", self.world.scan), (devices, "open_device", self.world.open),
                (devices, "find_leds", lambda key: []), (devices, "find_battery", lambda key: None),
                (select, "select", spy_select)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.addCleanup(self._ensure_stopped)

    def fast_settings(self):
        settings.SCAN_INTERVAL = 0.05
        settings.RETRY_DELAY = 0.05
        settings.RECONNECT_DELAY = 0.05
        settings.KEYBOARD_SETTLE_SECONDS = 0
        settings.STATE_SAVE_DELAY = 0.05

    def configure(self, text=None):
        """(Re)load config.toml (written from ``text`` if given) the way the daemon does at start."""
        if text is not None:
            self.config_path.write_text(text)
        warnings = settings.configure(self.config_path, self.overrides_path)
        settings.FEEDBACK_NOTIFY = False
        self.fast_settings()
        warnings += self.daemon.reload_profiles()
        return warnings

    def start(self):
        if self.serve and self.daemon.server is None:
            self.daemon.start_server(self.socket_path)
        self.thread.start()

    def stop(self):
        self.shutdown.request()
        self.thread.join(timeout=3.0)
        assert not self.thread.is_alive(), "run() did not stop on shutdown"
        self.daemon.close()

    def _ensure_stopped(self):
        if self.thread.is_alive():
            self.shutdown.request()
            self.thread.join(timeout=3.0)
        if self.daemon.server is not None:
            self.daemon.server.close()

    def client(self):
        from wiimote_mouse.client import Client
        client = Client(self.socket_path).connect()
        self.addCleanup(client.close)
        return client

    def calibrate(self, gyro, t0=5000.0):
        for i in range(100):
            gyro.push(*stationary_report(t0 + i * 0.01))
            time.sleep(0.001)
        assert wait_for(lambda: not self.controller.calibrating), "calibration did not finish"

    def connected(self, *args, **kwargs):
        """Add a remote, start the daemon and wait until the remote is grabbed."""
        devices_ = self.world.add_remote("AA", "/b1", *args, **kwargs)
        self.start()
        assert wait_for(lambda: devices_[0].grabbed), "the remote was never grabbed"
        return devices_
