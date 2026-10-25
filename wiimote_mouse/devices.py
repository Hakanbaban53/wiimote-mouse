"""Finding, opening and reading the Wii Remote's input devices."""

import fcntl
import logging
import os
import struct
import time
from evdev import InputDevice
from evdev import ecodes
from evdev import list_devices
from glob import glob


log = logging.getLogger("wiimote-mouse")



BUTTONS_NAME = "Nintendo Wii Remote"


GYRO_NAME = "Nintendo Wii Remote Motion Plus"


ACCEL_NAME = "Nintendo Wii Remote Accelerometer"


SYSFS_INPUT = "/sys/class/input"


SYSFS_LEDS = "/sys/class/leds"


SYSFS_POWER = "/sys/class/power_supply"


BATTERY_FULL_CAPACITY = 200 * 100 // 255


EVIOCSCLOCKID = 0x400445A0      # _IOW('E', 0xa0, int)


class DeviceLost(Exception):
    """An input device disappeared (Wii Remote slept, disconnected, ...)."""


class RemoteUnavailable(Exception):
    """The remote exists but can't be used right now; worth retrying."""


# Extension devices exist only while the accessory is plugged in (hid-wiimote registers
# and removes them on hotplug). The kernel only streams their data while they are open.
EXTENSIONS = {
    "Nintendo Wii Remote Nunchuk": "nunchuk",
    "Nintendo Wii Remote Classic Controller": "classic",
    "Nintendo Wii Remote Pro Controller": "pro_controller",
    "Nintendo Wii Remote Balance Board": "balance_board",
    "Nintendo Wii Remote Drums": "drums",
    "Nintendo Wii Remote Guitar": "guitar",
    "Nintendo Wii Remote Turntable": "turntable",
}
SLOTS = {BUTTONS_NAME: "buttons", GYRO_NAME: "gyro", ACCEL_NAME: "accel", **EXTENSIONS}
EXTENSION_SLOTS = tuple(EXTENSIONS.values())


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _hid_parent(path):
    """Parent HID device of a sysfs class node (.../<hid device>/<class>/<node>).

    hid-wiimote sets neither uniq nor phys on its input devices, but every node
    it creates (input, LED, battery) hangs off the same HID device.
    """
    return os.path.dirname(os.path.dirname(os.path.realpath(path)))


def scan_remotes():
    """Find Wii Remote input nodes without opening anything.

    Names come from sysfs, so unrelated (or unreadable) event nodes are never
    touched. Returns {remote_key: {"buttons": path, "gyro": path, "accel": path,
    "nunchuk": path, ...}}.
    """
    found = {}
    for path in list_devices():
        base = f"{SYSFS_INPUT}/{os.path.basename(path)}/device"
        slot = SLOTS.get(_read_text(f"{base}/name"))
        if slot is None:
            continue
        found.setdefault(_hid_parent(base), {})[slot] = path
    return found


def find_leds(key):
    """brightness files of the four player LEDs of a remote (p0..p3), or []."""
    paths = {}
    for node in glob(f"{SYSFS_LEDS}/*:blue:p[0-3]"):
        if _hid_parent(node) == key:
            paths[int(node[-1])] = os.path.join(node, "brightness")
    return [paths[i] for i in range(4)] if len(paths) == 4 else []


def find_battery(key):
    """The sysfs `capacity` file of a remote's battery, or None."""
    for node in glob(f"{SYSFS_POWER}/wiimote_battery_*"):
        if _hid_parent(node) == key:
            return os.path.join(node, "capacity")
    return None


def battery_percent(capacity):
    """Kernel capacity (0..~78 for a fresh battery) -> 0..100."""
    return max(0.0, min(100.0, capacity / BATTERY_FULL_CAPACITY * 100.0))


def read_battery(path):
    """Blocks for a moment: the kernel asks the remote for a status report."""
    with open(path, encoding="ascii") as handle:
        return battery_percent(float(handle.read().strip()))


def use_monotonic_clock(dev):
    """Ask the kernel to stamp events with CLOCK_MONOTONIC (best effort)."""
    try:
        fcntl.ioctl(dev.fd, EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))
    except OSError:
        pass    # realtime stamps still work; gyro only uses differences


def open_device(path):
    dev = InputDevice(path)
    use_monotonic_clock(dev)
    return dev


def read_events(dev):
    """Read everything pending. EAGAIN -> nothing; anything else -> DeviceLost.

    InputDevice.read() is a generator: errors surface while iterating, so the
    iteration itself has to be inside the try block.
    """
    try:
        return list(dev.read())
    except BlockingIOError:
        return []
    except OSError as exc:
        raise DeviceLost(str(exc)) from exc


class Remote:
    """The evdev nodes belonging to one physical Wii Remote."""

    def __init__(self, key, buttons, gyro=None, accel=None):
        self.key = key
        self.buttons = buttons
        self.gyro = gyro
        self.accel = accel

    @staticmethod
    def _close(dev):
        if dev is not None:
            try:
                dev.close()
            except OSError:
                pass

    def drop_gyro(self):
        self._close(self.gyro)
        self.gyro = None

    def drop_accel(self):
        self._close(self.accel)
        self.accel = None

    def drop_sensors(self):
        self.drop_gyro()
        self.drop_accel()

    def close(self):
        self.drop_sensors()
        self._close(self.buttons)


def connect():
    """Open the first available remote, or return None if there isn't one."""
    remotes = {k: v for k, v in scan_remotes().items() if "buttons" in v}
    if not remotes:
        return None
    if len(remotes) > 1:
        log.warning("More than one Wii Remote found; using only one.")
    key = sorted(remotes)[0]
    paths = remotes[key]
    try:
        buttons = open_device(paths["buttons"])
    except PermissionError as exc:
        raise RemoteUnavailable(
            "Found a Wii Remote but have no permission to read it (is the udev rule "
            "installed? Run 'wiimote-mouse --doctor')."
        ) from exc
    except OSError as exc:
        raise RemoteUnavailable(f"Could not open the Wii Remote ({exc}); will retry.") from exc

    remote = Remote(key, buttons)
    attach_sensors(remote, want_accel=False, paths=paths)
    return remote


def attach_sensors(remote, want_accel, paths=None):
    """Open sensor nodes that exist but aren't open yet (they may appear late).

    Returns True if the MotionPlus was newly attached. The accelerometer is only opened
    on request: an open node makes the remote stream, which costs battery.
    """
    if paths is None:
        paths = scan_remotes().get(remote.key, {})
    gyro_new = False
    for slot, wanted in (("gyro", True), ("accel", want_accel)):
        if not wanted or getattr(remote, slot) is not None or slot not in paths:
            continue
        try:
            setattr(remote, slot, open_device(paths[slot]))
        except OSError as exc:
            log.debug("Could not open %s (%s); will retry.", slot, exc)
            continue
        gyro_new = gyro_new or slot == "gyro"
    return gyro_new


def extension_slots(paths):
    """Names of the accessories currently plugged into a remote, from its scanned nodes."""
    return tuple(slot for slot in EXTENSION_SLOTS if slot in paths)


def code_name(event_type, code):
    """Readable name of an evdev code, e.g. ABS_HAT0X or BTN_C."""
    names = ecodes.bytype.get(event_type, {}).get(code, f"{event_type}:{code}")
    return names[0] if isinstance(names, (list, tuple)) else names
