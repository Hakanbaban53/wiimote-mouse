"""The daemon: waits for a Wii Remote, turns its input into mouse and keyboard events,
and serves the local control socket used by wiimotectl and the UI.

Everything runs on one event loop (no threads except short-lived helpers), which keeps
behaviour deterministic: a control command, a gyro report and a timer never race.
"""

import argparse
import errno
import logging
import math
import os
import queue
import select
import subprocess
import threading
import time

from evdev import UInputError
from evdev import ecodes as E

from . import __version__, control, devices, doctor, dsp, feedback, output, profiles, runtime, settings
from .control import ApiError
from .devices import DeviceLost, RemoteUnavailable
from .runtime import EXIT_FAILURE, EXIT_OK, EXIT_SETUP, Shutdown, Timers, Wakeup

log = logging.getLogger("wiimote-mouse")

MOTION_INTERVAL = 1 / 30        # max rate of "motion" events to clients
EXT_INTERVAL = 1 / 30           # extension input is coalesced to this rate
ACCEL_AXES = (E.ABS_RX, E.ABS_RY, E.ABS_RZ)     # accelerometer x, y, z as hid-wiimote reports them
MOUSE_BUTTONS = {"mouse.left": E.BTN_LEFT, "mouse.right": E.BTN_RIGHT, "mouse.middle": E.BTN_MIDDLE}


class CommandRunner:
    """Runs ``exec:`` actions one at a time, in order (press/release pairs must not race)."""

    def __init__(self, timeout=10):
        self.timeout = timeout
        self._queue = queue.Queue()
        self._thread = None

    def submit(self, argv):
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="exec", daemon=True)
            self._thread.start()
        self._queue.put(argv)

    def _work(self):
        while True:
            argv = self._queue.get()
            try:
                subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=self.timeout, check=False)
            except FileNotFoundError:
                log.warning("Command not found: %s", argv[0])
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("Command failed: %s (%s)", argv[0], exc)


def _arg(args, name, kind, default=None, required=True):
    """Fetch and type-check one command argument."""
    if name not in args:
        if required and default is None:
            raise ApiError(f"missing '{name}'")
        return default
    value = args[name]
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise ApiError(f"'{name}' has the wrong type")
    return value


class Daemon:
    """Everything that outlives one connection of one remote."""

    def __init__(self, mouse, shutdown, state=None, keyboard_factory=None, commands=None):
        self.mouse = mouse
        self.keyboard = None
        self._keyboard_factory = keyboard_factory or (lambda: output.VirtualKeyboard())
        self._keyboard_failed = False
        self.shutdown = shutdown
        self.state = state or runtime.StateStore(os.devnull)
        self.controller = dsp.GyroMouse()
        self.profiles = profiles.ProfileManager()
        self.wakeup = Wakeup()
        self.timers = Timers()
        self.commands = commands or CommandRunner()
        self.server = None
        self.session = None
        self.config_path = None
        self.overrides_path = None
        self.started = time.monotonic()
        self._published = None
        self._save_pending = False
        self._commands = {
            "hello": self._cmd_hello, "status": self._cmd_status, "calibrate": self._cmd_calibrate,
            "config.get": self._cmd_config_get, "config.set": self._cmd_config_set,
            "config.reset": self._cmd_config_reset, "config.reload": self._cmd_config_reload,
            "profile.list": self._cmd_profile_list, "profile.get": self._cmd_profile_get,
            "profile.set": self._cmd_profile_set, "profile.cycle": self._cmd_profile_cycle,
            "profile.save": self._cmd_profile_save, "profile.delete": self._cmd_profile_delete,
            "rumble": self._cmd_rumble, "led": self._cmd_led, "sensors": self._cmd_sensors,
            "devices": self._cmd_devices,
        }

    # ---- setup
    def start_server(self, path=None):
        server = control.ControlServer(path or control.socket_path(), self.handle_command,
                                       self.snapshot, self.topics_changed)
        server.start()
        self.server = server

    def reload_profiles(self):
        warnings = self.profiles.rebuild(settings.raw_config_profiles(), settings.ui_profiles())
        for warning in warnings:
            log.warning("%s", warning)
        return warnings

    def restore_profile(self):
        name = self.state.get("profile")
        if isinstance(name, str) and name in self.profiles.profiles:
            self.profiles.activate(name)
        if self.profiles.get().uses_keys:
            self.ensure_keyboard()

    def ensure_keyboard(self):
        if self.keyboard is None and not self._keyboard_failed:
            try:
                self.keyboard = self._keyboard_factory()
            except (OSError, UInputError) as exc:
                self._keyboard_failed = True
                log.warning("Cannot create the virtual keyboard (%s); key actions are disabled.", exc)
            else:
                # The desktop needs a moment to notice a new input device; a key sent
                # before that is silently lost.
                time.sleep(settings.KEYBOARD_SETTLE_SECONDS)
        return self.keyboard

    def close(self):
        self.flush()
        if self.server is not None:
            self.server.close()
        if self.keyboard is not None:
            self.keyboard.close()

    # ---- saving
    def schedule_save(self):
        if not self._save_pending:
            self._save_pending = True
            self.timers.after(settings.STATE_SAVE_DELAY, self.flush)

    def flush(self):
        self._save_pending = False
        settings.flush_overrides()
        self.state.flush()

    # ---- state and events
    def wants(self, topic):
        return self.server is not None and self.server.has_subscribers(topic)

    def emit(self, topic, data):
        if self.server is not None:
            self.server.broadcast(topic, data)

    def snapshot(self):
        """The slowly-changing state clients display. Pushed as a "state" event on change."""
        session = self.session
        remote = None
        if session is not None:
            fb = session.feedback
            remote = {
                "id": os.path.basename(session.remote.key) or None,
                "mode": fb.state if fb is not None else None,
                "asleep": session.asleep,
                "sensors": {"gyro": session.remote.gyro is not None,
                            "accel": session.remote.accel is not None},
                "extensions": list(session.extensions),
                "battery": (round(fb.battery_percent) if fb is not None and fb.battery_percent is not None
                            else None),
                "leds": fb.leds.mask if fb is not None and fb.leds is not None else None,
            }
        return {"connected": session is not None, "remote": remote,
                "pointer": {"sensitivity": round(settings.SENSITIVITY, 2)},
                "profile": {"active": self.profiles.active, "available": self.profiles.names()}}

    def publish_state(self):
        snapshot = self.snapshot()
        if snapshot != self._published:
            self._published = snapshot
            self.emit("state", snapshot)

    def topics_changed(self):
        if self.session is not None:
            self.session.next_scan = 0.0        # open or close streams right away

    # ---- changes shared by buttons and commands
    def adjust_sensitivity(self, factor):
        new = min(settings.SENSITIVITY_MAX, max(settings.SENSITIVITY_MIN, settings.SENSITIVITY * factor))
        at_limit = new == settings.SENSITIVITY
        settings.set_override("pointer.sensitivity", new)
        log.info("Sensitivity: %.1f", new)
        if self.session is not None and self.session.feedback is not None:
            self.session.feedback.sensitivity_changed(new, at_limit)
        self.schedule_save()
        self.publish_state()

    def set_profile(self, name):
        try:
            self.profiles.activate(name)
        except KeyError:
            raise ApiError(f"unknown profile '{name}'") from None
        self._profile_changed()

    def cycle_profile(self, step):
        self.profiles.cycle(step)
        self._profile_changed()

    def _profile_changed(self):
        profile = self.profiles.get()
        log.info("Profile: %s", profile.name)
        if profile.uses_keys:
            self.ensure_keyboard()
        self.state.set("profile", profile.name)
        self.schedule_save()
        self.publish_state()

    def settings_changed(self):
        if self.session is not None:
            self.session.apply_settings()
        self.publish_state()

    def spawn(self, argv):
        self.commands.submit(list(argv))

    # ---- control commands
    def handle_command(self, client, cmd, args):
        method = self._commands.get(cmd)
        if method is None:
            raise ApiError(f"unknown command '{cmd}'")
        return method(args)

    def _need_session(self):
        if self.session is None:
            raise ApiError("no Wii Remote is connected")
        return self.session

    def _cmd_hello(self, args):
        return {"name": "wiimote-mouse", "version": __version__, "protocol": control.PROTOCOL_VERSION}

    def _cmd_status(self, args):
        status = self.snapshot()
        status["daemon"] = {"version": __version__, "protocol": control.PROTOCOL_VERSION,
                            "uptime": round(time.monotonic() - self.started, 1)}
        if self.session is not None:       # one-off scan so extensions are current without a subscriber
            self.session.refresh_extensions()
            status["remote"]["extensions"] = list(self.session.extensions)
        return status

    def _cmd_calibrate(self, args):
        self._need_session()
        self.controller.begin_calibration()
        return {}

    def _cmd_config_get(self, args):
        key = args.get("key")
        try:
            return {"options": settings.describe()} if key is None else settings.describe(key)
        except KeyError:
            raise ApiError(f"unknown setting '{key}'") from None

    def _cmd_config_set(self, args):
        key = _arg(args, "key", str)
        if "value" not in args:
            raise ApiError("missing 'value'")
        try:
            restart = settings.set_override(key, args["value"])
        except KeyError:
            raise ApiError(f"unknown setting '{key}'") from None
        except ValueError as exc:
            raise ApiError(f"'{key}' {exc}") from None
        self.schedule_save()
        self.settings_changed()
        result = settings.describe(key)
        result["restart_required"] = restart
        return result

    def _cmd_config_reset(self, args):
        key = _arg(args, "key", str)
        try:
            settings.reset_override(key)
        except KeyError:
            raise ApiError(f"unknown setting '{key}'") from None
        self.schedule_save()
        self.settings_changed()
        return settings.describe(key)

    def _cmd_config_reload(self, args):
        self.flush()
        try:
            warnings = settings.configure(self.config_path or settings.default_config_path(),
                                          self.overrides_path or settings.default_overrides_path())
        except settings.ConfigError as exc:
            raise ApiError(str(exc)) from None
        warnings += self.reload_profiles()
        self.settings_changed()
        return {"warnings": warnings}

    def _cmd_profile_list(self, args):
        return {"active": self.profiles.active,
                "profiles": [self.profiles.public(name) for name in self.profiles.names()]}

    def _cmd_profile_get(self, args):
        name = _arg(args, "name", str, default=self.profiles.active, required=False)
        try:
            data = self.profiles.public(name)
        except KeyError:
            raise ApiError(f"unknown profile '{name}'") from None
        data["ui"] = settings.ui_profiles().get(name)       # what was saved from wiimotectl / the UI
        return data

    def _cmd_profile_set(self, args):
        self.set_profile(_arg(args, "name", str))
        return self.profiles.public()

    def _cmd_profile_cycle(self, args):
        step = _arg(args, "delta", int, default=1, required=False)
        self.cycle_profile(1 if step >= 0 else -1)
        return self.profiles.public()

    def _cmd_profile_save(self, args):
        name = _arg(args, "name", str)
        if not profiles.NAME_PATTERN.match(name):
            raise ApiError("profile names use lowercase letters, digits, - and _ (max 32)")
        try:
            definition = profiles.clean_definition(args.get("profile", {}))
        except profiles.ProfileError as exc:
            raise ApiError(str(exc)) from None
        settings.set_ui_profile(name, definition)
        self.reload_profiles()
        if name == self.profiles.active:
            self._profile_changed()
        self.schedule_save()
        self.publish_state()
        return self.profiles.public(name)

    def _cmd_profile_delete(self, args):
        name = _arg(args, "name", str)
        if not settings.delete_ui_profile(name):
            raise ApiError(f"'{name}' was not saved from wiimotectl or the UI "
                           "(profiles from config.toml are edited in that file)")
        self.reload_profiles()          # falls back to "default" if the active profile vanished
        self.state.set("profile", self.profiles.active)
        self.schedule_save()
        self.publish_state()
        return {"deleted": name, "active": self.profiles.active}

    def _cmd_rumble(self, args):
        session = self._need_session()
        count = _arg(args, "count", int, default=1, required=False)
        if not 1 <= count <= 10:
            raise ApiError("'count' must be between 1 and 10")
        if session.feedback is None or session.feedback.rumble is None:
            raise ApiError("rumble is not available (disabled in the settings or unsupported)")
        session.feedback.buzz(count)
        return {}

    def _cmd_led(self, args):
        session = self._need_session()
        if "mask" not in args:
            raise ApiError("missing 'mask' (0-15, or null for automatic)")
        mask = args["mask"]
        if mask is not None and (not isinstance(mask, int) or isinstance(mask, bool) or not 0 <= mask <= 15):
            raise ApiError("'mask' must be 0-15 or null")
        if session.feedback is None or session.feedback.leds is None:
            raise ApiError("LEDs are not available (disabled in the settings, or no write permission)")
        session.feedback.override_leds(mask)
        return {"leds": session.feedback.leds.mask}

    def _cmd_sensors(self, args):
        session = self._need_session()
        action = _arg(args, "action", str)
        if action == "sleep":
            session.sleep_sensors()
        elif action == "wake":
            session.wake()
        else:
            raise ApiError("'action' must be 'sleep' or 'wake'")
        return {"asleep": session.asleep}

    def _cmd_devices(self, args):
        return {"remotes": {os.path.basename(key) or key: paths for key, paths in devices.scan_remotes().items()}}

    # ---- waiting without a remote
    def _timer_timeout(self, timeout):
        deadline = self.timers.next_deadline()
        if deadline is None:
            return timeout
        until = max(0.0, deadline - time.monotonic())
        return until if timeout is None else min(timeout, until)

    def after_select(self, readable, writable):
        """Everything that happens after select() returns, for both loops."""
        if self.shutdown.r in readable:
            self.shutdown.drain()
        if self.wakeup.r in readable:
            self.wakeup.drain()
        if self.server is not None:
            self.server.service(readable, writable)
        self.timers.run_due(time.monotonic())

    def wait(self, seconds):
        """Sleep, but keep serving control clients. Returns True if shutdown was requested."""
        if self.shutdown.requested:
            return True
        read = [self.shutdown.r, self.wakeup.r] + (self.server.read_fds() if self.server else [])
        write = self.server.write_fds() if self.server else []
        readable, writable, _ = select.select(read, write, [], self._timer_timeout(seconds))
        self.after_select(readable, writable)
        return self.shutdown.requested

    def run(self):
        """Wait for a remote, serve it, and start over whenever it goes away."""
        waiting_logged = False
        last_problem = None

        def problem(message):
            nonlocal last_problem
            if message != last_problem:
                log.warning(message)
                last_problem = message

        while not self.shutdown.requested:
            try:
                remote = devices.connect()
            except RemoteUnavailable as exc:
                problem(str(exc))
                self.wait(settings.RETRY_DELAY)
                continue
            if remote is None:
                if not waiting_logged:
                    log.info("Waiting for a Wii Remote (press a button on it to connect)...")
                    waiting_logged = True
                self.wait(settings.SCAN_INTERVAL)
                continue

            waiting_logged = False
            session = Session(remote, self)
            self.session = session
            self.publish_state()
            backoff = 0.0
            try:
                session.run()
            except DeviceLost as exc:
                log.warning("Lost the Wii Remote (%s).", exc)
                backoff = settings.RECONNECT_DELAY
            except RemoteUnavailable as exc:
                problem(str(exc))
                backoff = settings.RETRY_DELAY
            finally:
                if session.established:
                    last_problem = None     # it worked; a later problem is news again
                session.close_inputs()
                self.session = None
                self.controller.disconnect()
                remote.close()
                self.publish_state()
            if backoff:
                self.wait(backoff)          # never spin on a remote whose nodes linger


class Session:
    """One connection of one remote to the long-lived daemon."""

    def __init__(self, remote, daemon):
        self.remote = remote
        self.daemon = daemon
        self.mouse = daemon.mouse
        self.controller = daemon.controller
        self.shutdown = daemon.shutdown
        self.scroll = dsp.ScrollRepeater()
        self.held = {}                  # button -> Binding as it was when pressed
        self.feedback = None
        self.battery = None
        self.established = False        # True once the remote is grabbed and in use
        self.asleep = False             # sensors closed to save the remote's battery
        self.last_activity = time.monotonic()
        self.next_scan = 0.0
        self.accel_raw = dict.fromkeys(ACCEL_AXES, 0.0)
        self.extensions = ()
        self.ext_devices = {}           # slot -> open device, only while a client watches "ext"
        self._ext_pending = {}
        self._ext_flush_scheduled = False
        self._last_motion = 0.0
        self._motion_dx = 0
        self._motion_dy = 0

    # ---- lifecycle
    def run(self):
        remote, daemon = self.remote, self.daemon
        try:
            remote.buttons.grab()
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise RemoteUnavailable(
                    "The Wii Remote is in use by another program (or another copy of this one).") from exc
            raise DeviceLost(str(exc)) from exc

        self.established = True
        try:
            log.info("Wii Remote connected.")
            self.feedback = feedback.Feedback.create(remote, daemon.timers, settings.SENSITIVITY)
            self.feedback.on_change = daemon.publish_state
            self.controller.on_event = self._on_controller_event
            self._sync_battery()
            if remote.gyro is None:
                log.info("Waiting for the MotionPlus (the buttons work meanwhile)...")
                self.feedback.waiting()
            else:
                log.info("Hold the remote still while it calibrates...")
                self.feedback.calibrating()
            self.last_activity = time.monotonic()
            self.next_scan = 0.0
            daemon.publish_state()
            self._loop()
        finally:
            self._cleanup()

    def _cleanup(self):
        self.controller.on_event = None
        if self.battery is not None:
            self.battery.stop()
        if self.feedback is not None:
            self.feedback.on_change = None
            self.feedback.close()
        for slot in list(self.ext_devices):
            self._close_ext(slot)
        try:
            self.remote.buttons.ungrab()
        except OSError:
            pass    # device already gone; closing the fd releases the grab

    def close_inputs(self):
        """Let go of everything this remote was holding down (called when it goes away)."""
        for name in list(self.held):
            binding = self.held.pop(name)
            try:
                self._release(binding)
            except Exception:
                log.exception("Releasing '%s' failed", name)
        self.scroll.release_all()
        self.mouse.release_all()
        if self.daemon.keyboard is not None:
            self.daemon.keyboard.release_all()

    def apply_settings(self):
        """Settings changed at runtime: re-evaluate everything that depends on them."""
        self.next_scan = 0.0
        if self.feedback is not None:
            self.feedback.reconfigure()
            self.feedback.sensitivity_changed(settings.SENSITIVITY)
        self._sync_battery()

    def _sync_battery(self):
        wanted = settings.FEEDBACK_NOTIFY or settings.FEEDBACK_RUMBLE
        if wanted and self.battery is None:
            path = devices.find_battery(self.remote.key)
            if path is not None:
                self.battery = feedback.BatteryMonitor(
                    lambda: devices.read_battery(path), self.daemon.wakeup.notify,
                    settings.BATTERY_POLL_INTERVAL, settings.BATTERY_FIRST_POLL_DELAY)
                self.battery.start()
        elif not wanted and self.battery is not None:
            self.battery.stop()
            self.battery = None

    # ---- main loop
    def _loop(self):
        remote, daemon = self.remote, self.daemon
        while not self.shutdown.requested:
            now = time.monotonic()
            self._maintain(now)
            read = [self.shutdown.r, daemon.wakeup.r, remote.buttons]
            for device in (remote.gyro, remote.accel, *self.ext_devices.values()):
                if device is not None:
                    read.append(device)
            if daemon.server is not None:
                read.extend(daemon.server.read_fds())
            write = daemon.server.write_fds() if daemon.server is not None else []
            try:
                readable, writable, _ = select.select(read, write, [], self._timeout(now))
            except (OSError, ValueError) as exc:
                raise DeviceLost(str(exc)) from exc

            daemon.after_select(readable, writable)
            if self.shutdown.requested:
                break
            self._handle_battery()
            if remote.buttons in readable:
                for event in devices.read_events(remote.buttons):
                    self._on_button(event)
            if remote.gyro is not None and remote.gyro in readable:
                self._read_gyro()
            if remote.accel is not None and remote.accel in readable:
                self._read_accel()
            for slot, device in list(self.ext_devices.items()):
                if device in readable:
                    self._read_ext(slot)
            for wheel, direction in self.scroll.repeats(time.monotonic()):
                self._scroll(wheel, direction)

    def _wants_accel(self):
        return settings.ROLL_COMPENSATION or self.daemon.wants("motion")

    def _watching(self):
        return self.daemon.wants("state") or self.daemon.wants("ext")

    def _sensors_missing(self):
        return not self.asleep and (self.remote.gyro is None
                                    or (self._wants_accel() and self.remote.accel is None))

    def _scan_needed(self):
        return self._sensors_missing() or self._watching()

    def _idle_deadline(self):
        if (settings.IDLE_SLEEP_SECONDS > 0 and not self.asleep and self.remote.gyro is not None
                and not self.controller.active and not self.daemon.wants("motion")):
            return self.last_activity + settings.IDLE_SLEEP_SECONDS
        return None

    def _maintain(self, now):
        """Housekeeping that runs before every wait."""
        if self.controller.active:
            self.last_activity = now
        if self.asleep and self.daemon.wants("motion"):
            self.wake()                      # a live view needs the sensors
        if self.remote.accel is not None and not self._wants_accel():
            self.remote.drop_accel()
            self.controller.roll.reset()
            self.controller.last_accel = None
            self.daemon.publish_state()
        if self.ext_devices and not self.daemon.wants("ext"):
            for slot in list(self.ext_devices):     # nobody is watching: let the remote stop streaming
                self._close_ext(slot)
        if now >= self.next_scan and self._scan_needed():
            self._scan(now)
        idle = self._idle_deadline()
        if idle is not None and now >= idle:
            self.sleep_sensors()

    def _timeout(self, now):
        """How long select() may sleep. None = until something happens."""
        waits = []
        for deadline in (self.scroll.next_deadline(), self.daemon.timers.next_deadline(),
                         self._idle_deadline()):
            if deadline is not None:
                waits.append(deadline - now)
        if self._scan_needed():
            waits.append(self.next_scan - now)
        return max(0.0, min(waits)) if waits else None

    def _scan(self, now):
        self.next_scan = now + settings.SCAN_INTERVAL
        paths = devices.scan_remotes().get(self.remote.key, {})
        sensors_before = (self.remote.gyro is not None, self.remote.accel is not None)
        if not self.asleep and devices.attach_sensors(self.remote, self._wants_accel(), paths):
            log.info("MotionPlus connected. Hold the remote still while it calibrates...")
            self.controller.gyro_lost()
        changed = sensors_before != (self.remote.gyro is not None, self.remote.accel is not None)
        extensions = devices.extension_slots(paths)
        if extensions != self.extensions:
            log.info("Extensions: %s", ", ".join(extensions) or "none")
            self.extensions = extensions
            changed = True
        self._sync_ext(paths)
        if changed:
            self.daemon.publish_state()

    def refresh_extensions(self):
        """One-off scan for the status command."""
        self.extensions = devices.extension_slots(devices.scan_remotes().get(self.remote.key, {}))

    # ---- power
    def sleep_sensors(self):
        if self.asleep:
            return
        log.info("Idle: closing the MotionPlus to save battery; any button wakes it.")
        self.remote.drop_sensors()
        self.controller.gyro_lost(silent=True)
        self.accel_raw = dict.fromkeys(ACCEL_AXES, 0.0)
        self.asleep = True
        self.feedback.asleep()

    def wake(self):
        if not self.asleep:
            return
        log.info("Waking up: reopening the MotionPlus...")
        self.asleep = False
        self.next_scan = 0.0
        self.feedback.waiting()

    def _touch(self):
        """Real button activity: restart the idle clock and wake sleeping sensors."""
        self.last_activity = time.monotonic()
        self.wake()

    # ---- sensors
    def _read_gyro(self):
        try:
            events = devices.read_events(self.remote.gyro)
        except DeviceLost as exc:
            log.warning("Lost the MotionPlus (%s); waiting for it to come back.", exc)
            self.remote.drop_gyro()
            self.controller.gyro_lost(silent=True)
            self.feedback.waiting()
            self.next_scan = time.monotonic() + settings.SCAN_INTERVAL
            return
        for event in events:
            self._on_gyro(event)

    def _read_accel(self):
        try:
            events = devices.read_events(self.remote.accel)
        except DeviceLost as exc:
            log.warning("Lost the accelerometer (%s); roll compensation paused.", exc)
            self.remote.drop_accel()
            self.controller.roll.reset()
            self.next_scan = time.monotonic() + settings.SCAN_INTERVAL
            return
        for event in events:
            self._on_accel(event)

    def _on_gyro(self, event):
        if event.type == E.EV_ABS and event.code in dsp.AXES:
            self.controller.raw[event.code] = float(event.value)
        elif event.type == E.EV_SYN:
            if event.code == E.SYN_REPORT:
                dx, dy, hires, steps = self.controller.frame(event.timestamp())
                self.mouse.emit(dx, dy, hires, steps)
                if self.daemon.wants("motion"):
                    self._emit_motion(dx, dy)
            elif event.code == E.SYN_DROPPED:
                self.controller.reset_motion()

    def _on_accel(self, event):
        axes = ACCEL_AXES
        if event.type == E.EV_ABS and event.code in axes:
            self.accel_raw[event.code] = float(event.value)
        elif event.type == E.EV_SYN and event.code == E.SYN_REPORT:
            self.controller.feed_accel(self.accel_raw[axes[0]], self.accel_raw[axes[1]],
                                       self.accel_raw[axes[2]], event.timestamp())

    def _emit_motion(self, dx, dy):
        """Live gyro/tilt data for clients, at most MOTION_INTERVAL apart."""
        self._motion_dx += dx
        self._motion_dy += dy
        now = time.monotonic()
        if now - self._last_motion < MOTION_INTERVAL:
            return
        self._last_motion = now
        controller = self.controller
        rates = controller.rates_dps()
        self.daemon.emit("motion", {
            "yaw": round(rates[0], 2) if rates else None,
            "pitch": round(rates[1], 2) if rates else None,
            "dx": self._motion_dx, "dy": self._motion_dy,
            "clutch": controller.b_held, "scroll": controller.a_held,
            "calibrating": controller.calibrating,
            "roll": round(math.degrees(controller.roll.angle), 1) if controller.roll.valid else None,
            "accel": [round(v, 1) for v in controller.last_accel] if controller.last_accel else None,
        })
        self._motion_dx = self._motion_dy = 0

    # ---- extensions (Nunchuk, Classic Controller, ...)
    def _sync_ext(self, paths):
        wanted = self.daemon.wants("ext")
        for slot in list(self.ext_devices):
            if not wanted or slot not in paths:
                self._close_ext(slot)
        if not wanted:
            return
        for slot in self.extensions:
            if slot not in self.ext_devices:
                try:
                    self.ext_devices[slot] = devices.open_device(paths[slot])
                except OSError as exc:
                    log.debug("Could not open %s (%s); will retry.", slot, exc)

    def _close_ext(self, slot):
        device = self.ext_devices.pop(slot, None)
        self._ext_pending.pop(slot, None)
        if device is not None:
            try:
                device.close()
            except OSError:
                pass

    def _read_ext(self, slot):
        try:
            events = devices.read_events(self.ext_devices[slot])
        except DeviceLost:
            self._close_ext(slot)
            self.next_scan = time.monotonic() + settings.SCAN_INTERVAL     # don't spin on a broken node
            return
        for event in events:
            if event.type in (E.EV_KEY, E.EV_ABS):
                self._ext_pending.setdefault(slot, {})[devices.code_name(event.type, event.code)] = event.value
        if self._ext_pending and not self._ext_flush_scheduled:
            self._ext_flush_scheduled = True
            self.daemon.timers.after(EXT_INTERVAL, self._flush_ext)

    def _flush_ext(self):
        self._ext_flush_scheduled = False
        pending, self._ext_pending = self._ext_pending, {}
        for slot, values in pending.items():
            self.daemon.emit("ext", {"device": slot, "values": values})

    # ---- feedback
    def _on_controller_event(self, name):
        if self.feedback is None:
            return
        if name == "calibration_started":
            self.feedback.calibrating()
        elif name == "calibrated":
            self.feedback.ready()

    def _handle_battery(self):
        if self.battery is None or self.feedback is None:
            return
        for percent in self.battery.poll():
            self.feedback.battery(percent)

    # ---- buttons and actions
    def _scroll(self, wheel, direction):
        self.mouse.scroll(wheel, direction, direction * 120 if settings.HIRES_SCROLL else 0)

    def _resync_buttons(self):
        """After SYN_DROPPED we may have missed releases: let go of everything that is no
        longer physically down so nothing stays stuck (clutch, clicks, keys, scrolling)."""
        try:
            down = {profiles.BUTTON_CODES[c] for c in self.remote.buttons.active_keys()
                    if c in profiles.BUTTON_CODES}
        except OSError as exc:
            raise DeviceLost(str(exc)) from exc
        for name in list(self.held):
            if name not in down:
                self.daemon.emit("buttons", {"button": name, "pressed": False})
                self._release(self.held.pop(name))

    def _on_button(self, event):
        if event.type == E.EV_SYN:
            if event.code == E.SYN_DROPPED:
                self._resync_buttons()
            return
        if event.type != E.EV_KEY or event.value == 2:
            return
        name = profiles.BUTTON_CODES.get(event.code)
        if name is None:
            return
        self._touch()
        pressed = event.value == 1
        self.daemon.emit("buttons", {"button": name, "pressed": pressed})
        if pressed:
            binding = self.daemon.profiles.binding(name)
            if binding is not None:
                self.held[name] = binding
                self._press(binding)
        else:
            binding = self.held.pop(name, None)
            if binding is not None:
                self._release(binding)

    def _press(self, binding):
        if binding.hold is not None:
            self._start(binding.hold)
        if binding.on_press is not None:
            self._tap(binding.on_press)

    def _release(self, binding):
        if binding.hold is not None:
            self._stop(binding.hold)
        if binding.on_release is not None:
            self._tap(binding.on_release)

    def _start(self, action):
        if action.kind == "keys":
            keyboard = self.daemon.ensure_keyboard()
            if keyboard is not None:
                keyboard.press(action.codes)
        elif action.kind == "exec":
            self.daemon.spawn(action.argv)
        elif action.kind == "profile":
            try:
                self.daemon.set_profile(action.name)
            except ApiError as exc:
                log.warning("%s", exc)
        else:
            self._start_simple(action.name)

    def _start_simple(self, name):
        if name == "pointer.hold":
            self.controller.switch_mode("B", True)
        elif name == "scroll.hold":
            self.controller.switch_mode("A", True)
        elif name in MOUSE_BUTTONS:
            self.mouse.button(MOUSE_BUTTONS[name], True)
        elif name in dsp.SCROLL_TOKENS:
            for wheel, direction in self.scroll.changed(name, True, time.monotonic()):
                self._scroll(wheel, direction)
        elif name == "sensitivity.up":
            self.daemon.adjust_sensitivity(1.12)
        elif name == "sensitivity.down":
            self.daemon.adjust_sensitivity(1 / 1.12)
        elif name == "calibrate":
            self.controller.begin_calibration()
        elif name == "profile.next":
            self.daemon.cycle_profile(1)
        elif name == "profile.previous":
            self.daemon.cycle_profile(-1)

    def _stop(self, action):
        if action.kind == "keys":
            if self.daemon.keyboard is not None:
                self.daemon.keyboard.release(action.codes)
        elif action.kind == "simple":
            name = action.name
            if name == "pointer.hold":
                self.controller.switch_mode("B", False)
            elif name == "scroll.hold":
                self.controller.switch_mode("A", False)
            elif name in MOUSE_BUTTONS:
                self.mouse.button(MOUSE_BUTTONS[name], False)
            elif name in dsp.SCROLL_TOKENS:
                self.scroll.changed(name, False, time.monotonic())

    def _tap(self, action):
        """One-shot trigger (the {press, release} table form)."""
        if action.kind == "keys":
            keyboard = self.daemon.ensure_keyboard()
            if keyboard is not None:
                keyboard.tap(action.codes)
        elif action.kind == "simple" and action.name in MOUSE_BUTTONS:
            self.mouse.button(MOUSE_BUTTONS[action.name], True)
            self.mouse.button(MOUSE_BUTTONS[action.name], False)
        elif action.kind == "simple" and action.name in dsp.SCROLL_TOKENS:
            wheel, direction = dsp.SCROLL_TOKENS[action.name]
            self._scroll(wheel, direction)
        elif action.kind == "simple" and action.name in ("pointer.hold", "scroll.hold"):
            log.debug("'%s' only makes sense for a button that is held; ignored in press/release", action.name)
        else:
            self._start(action)


# ======================= Entry point =======================

def open_virtual_mouse(shutdown):
    """Create the uinput mouse. Right after login the seat ACL for /dev/uinput may
    not be applied yet, so retry for a while before giving up."""
    deadline = time.monotonic() + settings.UINPUT_WAIT_SECONDS
    announced = False
    while True:
        try:
            return output.VirtualMouse()
        except (OSError, UInputError) as exc:
            if shutdown.requested:
                return None
            if time.monotonic() >= deadline:
                raise
            if not announced:
                log.warning("/dev/uinput is not usable yet (%s); will keep trying for %d s...",
                            exc, settings.UINPUT_WAIT_SECONDS)
                announced = True
            shutdown.sleep(1.0)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="wiimote-mouse", description="Use a Wii Remote with MotionPlus as a mouse (daemon).")
    parser.add_argument("-c", "--config", help="config file (default: ~/.config/wiimote-mouse/config.toml)")
    parser.add_argument("--check-config", action="store_true", help="validate the configuration and exit")
    parser.add_argument("--doctor", action="store_true", help="check the installation and device access, then exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"wiimote-mouse {__version__}")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[%(levelname)s] %(message)s")
    config_path = args.config or settings.default_config_path()
    overrides_path = settings.default_overrides_path()
    try:
        warnings = settings.configure(config_path, overrides_path)
    except settings.ConfigError as exc:
        log.error("%s", exc)
        return EXIT_SETUP
    manager = profiles.ProfileManager()
    warnings += manager.rebuild(settings.raw_config_profiles(), settings.ui_profiles())
    for warning in warnings:
        log.warning("%s", warning)
    if args.check_config:
        found = os.path.exists(config_path)
        print((f"Configuration is valid: {config_path}" if not warnings else
               f"Configuration loaded with warnings: {config_path}") if found else
              f"No config file at {config_path}; defaults will be used.", flush=True)
        return EXIT_FAILURE if warnings else EXIT_OK
    if args.doctor:
        return doctor.run(config_path, warnings)

    shutdown = Shutdown()
    shutdown.install()
    try:
        mouse = open_virtual_mouse(shutdown)
    except (OSError, UInputError) as exc:
        log.error("Could not create the virtual mouse: %s", exc)
        log.error("Access to /dev/uinput is required: load the 'uinput' module and allow "
                  "access (udev rule). Run: wiimote-mouse --doctor")
        return EXIT_SETUP
    if mouse is None:
        return EXIT_OK

    print(f"Wii MotionPlus Mouse v{__version__}", flush=True)
    print("B: move | 1: left click | 2: right click | D-pad: scroll", flush=True)
    print("A + tilt: smooth scroll | +/-: sensitivity | HOME: recalibrate", flush=True)
    print("Control it with: wiimotectl --help   (quit with Ctrl+C)", flush=True)

    state = runtime.StateStore(settings.default_state_path())
    state.load()
    daemon = Daemon(mouse, shutdown, state)
    daemon.config_path, daemon.overrides_path = config_path, overrides_path
    daemon.reload_profiles()
    daemon.restore_profile()
    try:
        daemon.start_server()
    except control.AddressInUse as exc:
        log.error("%s", exc)
        mouse.close()
        return EXIT_SETUP
    except OSError as exc:
        log.warning("Control socket unavailable (%s); running without wiimotectl/UI support.", exc)

    status = EXIT_OK
    try:
        daemon.run()
    except KeyboardInterrupt:
        pass
    except OSError as exc:      # virtual mouse write failed: nothing to recover to
        log.error("Writing to the virtual mouse failed: %s", exc)
        status = EXIT_FAILURE
    finally:
        daemon.close()
        mouse.close()
    log.info("Stopped.")
    return status
