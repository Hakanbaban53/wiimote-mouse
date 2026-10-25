"""Starting up: /dev/uinput races, setup errors, real signals, and diagnostics."""

import contextlib
import errno
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from wiimote_mouse import control, daemon, devices, doctor, ctl, output, runtime, settings
from tests.helpers import DaemonCase, RecordingMouse, SettingsIsolation, temp_dir

ROOT = Path(__file__).resolve().parent.parent


class UinputStartupTests(SettingsIsolation):
    class Stub:
        requested = False

        def sleep(self, seconds):
            time.sleep(0.01)

    def test_login_race_is_retried(self):
        calls = []

        def factory():
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(errno.EACCES, "ACL not applied yet")
            return "mouse"

        with mock.patch.object(output, "VirtualMouse", factory):
            self.assertEqual(daemon.open_virtual_mouse(self.Stub()), "mouse")
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_the_deadline(self):
        settings.UINPUT_WAIT_SECONDS = 0.05
        with mock.patch.object(output, "VirtualMouse", side_effect=FileNotFoundError("no uinput")):
            with self.assertRaises(FileNotFoundError):
                daemon.open_virtual_mouse(self.Stub())

    def test_shutdown_during_the_wait_is_a_clean_exit(self):
        stub = self.Stub()
        stub.requested = True
        with mock.patch.object(output, "VirtualMouse", side_effect=PermissionError("denied")):
            self.assertIsNone(daemon.open_virtual_mouse(stub))


class MainTests(SettingsIsolation):
    def setUp(self):
        super().setUp()
        self.dir = temp_dir(self)
        env = {"XDG_CONFIG_HOME": self.dir, "XDG_STATE_HOME": self.dir, "XDG_RUNTIME_DIR": self.dir}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("install", lambda self: None), ("sleep", lambda self, s: time.sleep(0.01))):
            patch = mock.patch.object(runtime.Shutdown, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def write_config(self, text):
        path = Path(self.dir) / "wiimote-mouse" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    def test_check_config_exit_codes(self):
        good = self.write_config("[pointer]\nsensitivity = 20\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(daemon.main(["--check-config", "-c", good]), runtime.EXIT_OK)
            self.assertIn("Configuration is valid", out.getvalue())
            self.write_config("[pointer]\nnope = 1\n")
            self.assertEqual(daemon.main(["--check-config", "-c", good]), runtime.EXIT_FAILURE)
            self.assertIn("with warnings", out.getvalue())
            self.assertEqual(daemon.main(["--check-config", "-c", "/nonexistent.toml"]), runtime.EXIT_OK)
            self.assertIn("No config file", out.getvalue())
        self.write_config("[[[")
        self.assertEqual(daemon.main(["--check-config", "-c", good]), runtime.EXIT_SETUP)

    def test_profile_problems_are_reported_by_check_config(self):
        path = self.write_config('[profiles.x.buttons]\na = "fly"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daemon.main(["--check-config", "-c", path]), runtime.EXIT_FAILURE)

    def test_missing_uinput_is_a_setup_error_with_exit_code_2(self):
        settings.UINPUT_WAIT_SECONDS = 0.05
        with mock.patch.object(output, "VirtualMouse", side_effect=PermissionError("denied")), \
                self.assertLogs("wiimote-mouse", level="ERROR"):
            self.assertEqual(daemon.main([]), runtime.EXIT_SETUP)

    def test_a_second_daemon_refuses_to_start(self):
        first = control.ControlServer(control.socket_path(), lambda *a: {})
        first.start()
        self.addCleanup(first.close)
        mouse = RecordingMouse()
        with mock.patch.object(output, "VirtualMouse", lambda: mouse), \
                self.assertLogs("wiimote-mouse", level="ERROR") as logs:
            self.assertEqual(daemon.main([]), runtime.EXIT_SETUP)
        self.assertTrue(any("already running" in m for m in logs.output))

    def test_an_unusable_control_socket_only_disables_the_clients(self):
        mouse = RecordingMouse()
        with mock.patch.object(output, "VirtualMouse", lambda: mouse), \
                mock.patch.object(daemon.Daemon, "start_server", side_effect=PermissionError("no")), \
                mock.patch.object(daemon.Daemon, "run", lambda self: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertLogs("wiimote-mouse", level="WARNING") as logs:
            self.assertEqual(daemon.main([]), runtime.EXIT_OK)
        self.assertTrue(any("Control socket unavailable" in m for m in logs.output))

    def test_the_saved_profile_and_overrides_are_loaded_at_start(self):
        Path(self.dir, "wiimote-mouse").mkdir(parents=True, exist_ok=True)
        Path(self.dir, "wiimote-mouse", "overrides.json").write_text(
            '{"settings": {"pointer.sensitivity": {"value": 44.0, "base": 30.0}}}')
        Path(self.dir, "wiimote-mouse", "state.json").write_text('{"profile": "media"}')
        seen = {}

        def fake_run(self):
            seen["profile"] = self.profiles.active
            seen["sensitivity"] = settings.SENSITIVITY

        with mock.patch.object(output, "VirtualMouse", RecordingMouse), \
                mock.patch.object(output, "VirtualKeyboard", RecordingMouse), \
                mock.patch.object(daemon.Daemon, "run", fake_run), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daemon.main([]), runtime.EXIT_OK)
        self.assertEqual(seen, {"profile": "media", "sensitivity": 44.0})


SERVER_SCRIPT = """
import sys
sys.path.insert(0, {root!r})
from wiimote_mouse import daemon, devices, output
from tests.helpers import FakeDevice, RecordingMouse, RecordingKeyboard
buttons, gyro = FakeDevice(), FakeDevice()
real_grab = buttons.grab
def grab():
    real_grab()
    print("READY", flush=True)
buttons.grab = grab
table = {{"/b": buttons, "/g": gyro}}
devices.scan_remotes = lambda: {{"/sys/x/0005:057E:0306.0001": {{"buttons": "/b", "gyro": "/g"}}}}
devices.open_device = table.__getitem__
devices.find_leds = lambda key: []
devices.find_battery = lambda key: None
output.VirtualMouse = RecordingMouse
output.VirtualKeyboard = RecordingKeyboard
sys.exit(daemon.main([]))
"""


class RealProcessTests(unittest.TestCase):
    """The daemon as a real process: real signals, real socket, real wiimotectl calls."""

    def run_daemon(self, sig):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = dict(os.environ, XDG_CONFIG_HOME=tmp.name, XDG_STATE_HOME=tmp.name, XDG_RUNTIME_DIR=tmp.name)
        proc = subprocess.Popen([sys.executable, "-c", SERVER_SCRIPT.format(root=str(ROOT))],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            for line in proc.stdout:
                if line.strip() == "READY":
                    break
            else:
                self.fail("never became ready: " + proc.stderr.read())
            socket_path = Path(tmp.name) / "wiimote-mouse" / "control.sock"
            deadline = time.monotonic() + 3
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status_code = ctl.main(["--socket", str(socket_path), "status"])
            started = time.monotonic()
            proc.send_signal(sig)
            proc.wait(timeout=3)
            return (proc.returncode, time.monotonic() - started, status_code, out.getvalue(),
                    socket_path.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def check(self, sig):
        code, elapsed, status_code, status_out, socket_left = self.run_daemon(sig)
        self.assertEqual(code, runtime.EXIT_OK)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(status_code, 0)
        self.assertIn("connected (0005:057E:0306.0001)", status_out)
        self.assertFalse(socket_left, "the control socket must be removed on shutdown")

    def test_sigterm_stops_cleanly_and_fast_and_wiimotectl_works_meanwhile(self):
        self.check(signal.SIGTERM)

    def test_sigint_stops_cleanly_and_fast(self):
        self.check(signal.SIGINT)


class DoctorTests(DaemonCase):
    serve = True

    def collect(self, **patches):
        with mock.patch.multiple(devices, **patches):
            return doctor.collect("/nonexistent/config.toml")

    def texts(self, checks):
        return [c.text for c in checks]

    def test_no_remote_is_a_warning_not_a_failure(self):
        checks = self.collect(scan_remotes=lambda: {})
        warning = next(c for c in checks if "No Wii Remote" in c.text)
        self.assertEqual(warning.status, doctor.WARN)
        self.assertIn("bluetoothctl", warning.hint)

    def test_healthy_remote_is_reported(self):
        directory = temp_dir(self)
        files = {}
        for name in ("buttons", "gyro", "accel", "led0", "led1", "led2", "led3", "capacity"):
            files[name] = os.path.join(directory, name)
            Path(files[name]).write_text("78\n")
        checks = self.collect(
            scan_remotes=lambda: {"/sys/x/0005:057E:0306.0001": {
                "buttons": files["buttons"], "gyro": files["gyro"], "accel": files["accel"],
                "nunchuk": files["accel"]}},
            find_leds=lambda key: [files[f"led{i}"] for i in range(4)],
            find_battery=lambda key: files["capacity"])
        text = "\n".join(self.texts(checks))
        for expected in ("Found a Wii Remote", "MotionPlus", "Player LEDs are writable", "Battery: about 100%",
                         "Extensions plugged in: nunchuk"):
            self.assertIn(expected, text)
        self.assertFalse(any(c.status == doctor.FAIL and "MotionPlus" in c.text for c in checks))

    def test_unreadable_nodes_fail_with_a_hint(self):
        checks = self.collect(scan_remotes=lambda: {"/sys/x/0005:057E:0306.0001": {"buttons": "/nonexistent/event9"}},
                              find_leds=lambda key: [], find_battery=lambda key: None)
        failure = next(c for c in checks if c.status == doctor.FAIL and "event9" in c.text)
        self.assertIn("udev rule", failure.hint)

    def test_a_running_daemon_is_detected(self):
        self.start()
        with mock.patch.object(doctor.control, "socket_path", lambda: self.socket_path):
            self.assertIsNotNone(doctor.daemon_version())
            checks = self.collect(scan_remotes=lambda: {})
        self.assertTrue(any("The daemon is running" in c.text for c in checks))

    def test_no_daemon(self):
        with mock.patch.object(doctor.control, "socket_path", lambda: self.dir / "none.sock"):
            self.assertIsNone(doctor.daemon_version())

    def test_config_warnings_are_listed(self):
        checks = doctor.collect(__file__, ["Unknown setting 'x'; ignored."])
        self.assertIn("Unknown setting 'x'; ignored.", self.texts(checks))

    def test_run_prints_a_report_and_returns_the_exit_code(self):
        failing = [doctor.Check(doctor.FAIL, "bad", "fix it")]
        out = io.StringIO()
        with mock.patch.object(doctor, "collect", return_value=failing), contextlib.redirect_stdout(out):
            self.assertEqual(doctor.run("x"), 1)
        self.assertIn("1 problem(s) found.", out.getvalue())
        with mock.patch.object(doctor, "collect", return_value=[doctor.Check(doctor.OK, "ok")]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.run("x"), 0)

    def test_systemd_state_without_systemctl(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertIsNone(doctor.systemd_state())


if __name__ == "__main__":
    unittest.main()
