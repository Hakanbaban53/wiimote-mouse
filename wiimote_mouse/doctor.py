"""Installation and device-access checks (``wiimote-mouse --doctor``, also used by the UI)."""

import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__, control, devices, settings

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    status: str             # "ok", "warn" or "fail"
    text: str
    hint: str = ""


def daemon_version():
    """Ask a running daemon for its version over the control socket, or return None."""
    path = control.socket_path()
    if not path.exists():
        return None
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(str(path))
        probe.sendall(b'{"id":1,"cmd":"hello"}\n')
        reply = json.loads(probe.makefile("rb").readline())
        return reply["result"]["version"] if reply.get("ok") else None
    except (OSError, ValueError, KeyError):
        return None
    finally:
        probe.close()


def collect(config_path, config_warnings=()):
    """Run every check. Returns a list of Check, in the order they should be shown."""
    checks = []

    def add(status, text, hint=""):
        checks.append(Check(status, text, hint))

    try:
        from importlib import metadata
        evdev_version = metadata.version("evdev")
    except Exception:
        evdev_version = "unknown"
    add(OK, f"wiimote-mouse {__version__}, Python {sys.version.split()[0]}, python-evdev {evdev_version}")

    if Path(config_path).exists():
        if config_warnings:
            for warning in config_warnings:
                add(WARN, warning)
        else:
            add(OK, f"Config is valid: {config_path}")
    else:
        add(OK, f"No config file ({config_path}); using the defaults")

    if not os.path.exists("/dev/uinput"):
        add(FAIL, "/dev/uinput is missing",
            "sudo modprobe uinput (install.sh makes it permanent via modules-load.d)")
    elif not os.access("/dev/uinput", os.W_OK):
        add(FAIL, "/dev/uinput is not writable",
            "install the udev rule (install.sh), then log out and back in")
    else:
        add(OK, "/dev/uinput is accessible")

    remotes = {key: paths for key, paths in devices.scan_remotes().items() if "buttons" in paths}
    if not remotes:
        add(WARN, "No Wii Remote is connected right now",
            "pair and trust it with bluetoothctl, then press a button on it")
    for key, paths in sorted(remotes.items()):
        add(OK, f"Found a Wii Remote ({os.path.basename(key)})")
        for slot, label in (("buttons", "buttons"), ("gyro", "MotionPlus"), ("accel", "accelerometer")):
            path = paths.get(slot)
            if path is None:
                if slot == "gyro":
                    add(WARN, "No MotionPlus device",
                        "cursor movement needs a MotionPlus (or Wii Remote Plus); if you have one, wait a moment")
                elif slot == "accel" and settings.ROLL_COMPENSATION:
                    add(WARN, "No accelerometer device", "roll compensation will not work")
                continue
            if os.access(path, os.R_OK | os.W_OK):
                add(OK, f"{label}: {path} is readable and writable")
            else:
                add(FAIL, f"{label}: {path} is not accessible",
                    "install the udev rule (install.sh) and log in again")
        extensions = devices.extension_slots(paths)
        if extensions:
            add(OK, "Extensions plugged in: " + ", ".join(extensions))
        leds = devices.find_leds(key)
        if not leds:
            add(WARN, "Player LEDs not found")
        elif all(os.access(path, os.W_OK) for path in leds):
            add(OK, "Player LEDs are writable")
        else:
            add(WARN, "No permission to write the player LEDs (only LED feedback is affected)",
                "join the 'wiimote' group (install.sh adds you) and log out and back in")
        battery = devices.find_battery(key)
        if battery is None:
            add(WARN, "No battery information")
        else:
            try:
                add(OK, f"Battery: about {devices.read_battery(battery):.0f}%")
            except (OSError, ValueError) as exc:
                add(WARN, f"Could not read the battery ({exc})")

    version = daemon_version()
    if version is not None:
        add(OK, f"The daemon is running (v{version}) and answers on {control.socket_path()}")
    else:
        add(WARN, "The daemon is not running (or its control socket is unreachable)",
            "systemctl --user enable --now wiimote-mouse.service")

    if shutil.which("notify-send"):
        add(OK, "notify-send is available (desktop notifications)")
    else:
        add(WARN, "notify-send is missing, so notifications will be off", "install libnotify")
    return checks


def run(config_path, config_warnings=()):
    """Print the report. Returns 0 if nothing failed, otherwise 1."""
    print(f"Wii MotionPlus Mouse v{__version__}: diagnostics\n")
    failed = 0
    for check in collect(config_path, config_warnings):
        print(f" {({'ok': '✓', 'warn': '!', 'fail': '✗'})[check.status]} {check.text}")
        if check.hint and check.status != OK:
            print(f"     → {check.hint}")
        failed += check.status == FAIL
    print()
    print("No problems found." if not failed else f"{failed} problem(s) found.")
    return 1 if failed else 0


def systemd_state():
    """State of the user service ("active", "inactive", ...) or None without systemd."""
    if not shutil.which("systemctl"):
        return None
    try:
        result = subprocess.run(["systemctl", "--user", "is-active", "wiimote-mouse.service"],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None
