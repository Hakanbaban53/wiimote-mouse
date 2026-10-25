"""wiimotectl against a running daemon."""

import contextlib
import io
import json
import sys
import threading
import types
import unittest
from unittest import mock

from evdev import ecodes as E

from wiimote_mouse import ctl, doctor, settings
from tests.helpers import DaemonCase, key
from tests.test_commands import fake_feedback


class CliCase(DaemonCase):
    serve = True

    def ctl(self, *argv, socket=True):
        """Run wiimotectl in-process. Returns (exit code, stdout, stderr)."""
        argv = (["--socket", str(self.socket_path)] if socket else []) + list(argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ctl.main(argv)
        return code, out.getvalue(), err.getvalue()


class StatusTests(CliCase):
    def test_status_without_a_remote(self):
        self.start()
        code, out, _ = self.ctl("status")
        self.assertEqual(code, 0)
        self.assertIn("Remote       not connected", out)
        self.assertIn("Profile      default", out)
        self.assertIn("Sensitivity  30.0", out)

    def test_status_with_a_remote_and_json(self):
        buttons, gyro, _ = self.connected("/g1", nunchuk="/n1")
        code, out, _ = self.ctl("status")
        self.assertEqual(code, 0)
        for expected in ("connected (AA)", "calibrating (hold the remote still)", "gyro on, accelerometer off",
                         "Extensions  nunchuk"):
            self.assertIn(expected, out)
        code, out, _ = self.ctl("--json", "status")
        data = json.loads(out)
        self.assertEqual((code, data["remote"]["id"], data["profile"]["active"]), (0, "AA", "default"))
        self.stop()

    def test_unreachable_daemon_gives_exit_code_3_and_a_hint(self):
        code, out, err = self.ctl("--socket", str(self.dir / "nothing.sock"), "status", socket=False)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("not running", err)
        self.assertIn("systemctl --user start", err)

    def test_usage_errors_exit_with_2(self):
        for argv in (["bogus"], [], ["config", "set", "pointer.sensitivity", "fast"], ["led", "x"], ["watch", "--topics", "nope"]):
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                ctl.main(argv)
            self.assertEqual(caught.exception.code, 2, argv)

    def test_help_and_version(self):
        for argv in (["--help"], ["config", "--help"], ["--version"]):
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(io.StringIO()) as out:
                ctl.main(argv)
            self.assertEqual(caught.exception.code, 0)
            self.assertTrue(out.getvalue())


class SettingsCommandTests(CliCase):
    def test_sensitivity(self):
        self.start()
        self.assertEqual(self.ctl("sensitivity")[1].strip(), "30.0")
        self.assertEqual(self.ctl("sensitivity", "45")[1].strip(), "Sensitivity: 45.0")
        self.assertEqual(settings.SENSITIVITY, 45.0)
        self.ctl("sensitivity", "up")
        self.assertAlmostEqual(settings.SENSITIVITY, 45 * 1.12, places=1)
        self.ctl("sensitivity", "down")
        self.assertAlmostEqual(settings.SENSITIVITY, 45.0, places=1)
        code, _, err = self.ctl("sensitivity", "99999")
        self.assertEqual(code, 1)
        self.assertIn("between", err)
        code, _, err = self.ctl("sensitivity", "warp")
        self.assertEqual(code, 1)
        self.assertIn("number", err)

    def test_config_commands(self):
        self.start()
        code, out, _ = self.ctl("config", "list")
        self.assertEqual(code, 0)
        self.assertIn("pointer.sensitivity", out)
        self.assertIn("tilt.roll_compensation", out)
        self.assertEqual(self.ctl("config", "get", "pointer.invert_x")[1].strip(), "true")
        self.assertEqual(self.ctl("config", "set", "pointer.invert_x", "false")[1].strip(),
                         "pointer.invert_x = false (override)")
        self.assertIs(settings.INVERT_X, False)
        self.assertIn("restarting", self.ctl("config", "set", "scroll.hires", "false")[1])
        self.assertEqual(self.ctl("config", "reset", "pointer.invert_x")[1].strip(), "pointer.invert_x = True (default)")
        code, _, err = self.ctl("config", "get", "nope")
        self.assertEqual((code, "unknown setting" in err), (1, True))
        self.config_path.write_text("[pointer]\nunknown = 1\n")
        code, out, err = self.ctl("config", "reload")
        self.assertEqual((code, "Configuration reloaded." in out, "warning: Unknown setting" in err), (0, True, True))

    def test_calibrate(self):
        buttons, gyro, _ = self.connected("/g1")
        self.calibrate(gyro)
        code, out, _ = self.ctl("calibrate")
        self.assertEqual(code, 0)
        self.assertIn("hold the remote still", out)
        self.assertTrue(self.controller.calibrating)
        self.stop()


class ProfileCommandCliTests(CliCase):
    def test_list_show_set_next_prev(self):
        self.start()
        out = self.ctl("profile", "list")[1]
        self.assertIn("* default", out)
        self.assertIn("presentation", out)
        self.assertEqual(self.ctl("profile", "set", "media")[1].strip(), "Active profile: media")
        self.assertEqual(self.ctl("profile", "next")[1].strip(), "Active profile: default")
        self.assertEqual(self.ctl("profile", "prev")[1].strip(), "Active profile: media")
        shown = self.ctl("profile", "show")[1]
        self.assertIn("media  (active)", shown)
        self.assertIn("* a      key:KEY_PLAYPAUSE", shown)          # own bindings are starred
        self.assertIn("  b      pointer.hold", shown)               # inherited ones are not
        code, _, err = self.ctl("profile", "set", "nope")
        self.assertEqual((code, "unknown profile" in err), (1, True))

    def test_bind_unbind_save_delete(self):
        self.start()
        self.assertIn("two -> key:KEY_F13", self.ctl("profile", "bind", "ptt", "two", "key:KEY_F13")[1])
        self.assertIn("home -> none", self.ctl("profile", "bind", "ptt", "home", "none")[1])
        shown = self.ctl("profile", "show", "ptt")[1]
        self.assertIn("* two    key:KEY_F13", shown)
        code, out, _ = self.ctl("--json", "profile", "show", "ptt")
        self.assertEqual(json.loads(out)["ui"]["buttons"], {"two": "key:KEY_F13", "home": "none"})
        self.ctl("profile", "unbind", "ptt", "home")
        self.assertNotIn("home", json.loads(self.ctl("--json", "profile", "show", "ptt")[1])["ui"]["buttons"])
        code, _, err = self.ctl("profile", "unbind", "ptt", "home")
        self.assertEqual((code, "no saved binding" in err), (1, True))
        self.assertIn("Removed the saved changes", self.ctl("profile", "unbind", "ptt", "two")[1])
        code, _, err = self.ctl("profile", "show", "ptt")
        self.assertEqual(code, 1)

        saved = self.ctl("profile", "save", "demo", "--description", "Demo", "--button", "a=calibrate",
                         "--button", 'two={"press": "exec:echo hi", "release": "exec:echo bye"}')
        self.assertEqual(saved[0], 0)
        two = json.loads(self.ctl("--json", "profile", "show", "demo")[1])["buttons"]["two"]
        self.assertEqual(two, {"press": "exec:echo hi", "release": "exec:echo bye"})
        self.assertEqual(self.ctl("profile", "delete", "demo")[1].strip(), "Deleted 'demo'.")
        code, _, err = self.ctl("profile", "delete", "media")
        self.assertEqual(code, 1)
        code, _, err = self.ctl("profile", "bind", "x", "a", "fly")
        self.assertEqual((code, "unknown action" in err), (1, True))

    def test_bad_assignments_are_usage_errors(self):
        self.start()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            ctl.main(["--socket", str(self.socket_path), "profile", "save", "x", "--button", "justtext"])
        self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            ctl.main(["--socket", str(self.socket_path), "profile", "bind", "x", "a", "{bad json"])
        self.assertEqual(caught.exception.code, 2)


class RemoteCommandCliTests(CliCase):
    def test_rumble_led_sensors(self):
        leds, rumble, notifier = fake_feedback(self)
        buttons, gyro, _ = self.connected("/g1")
        self.assertEqual(self.ctl("rumble", "2")[0], 0)
        self.assertEqual(self.ctl("led", "10")[1].strip(), "LEDs: 1010")
        self.assertEqual(self.ctl("led", "auto")[0], 0)
        self.assertEqual(self.ctl("sensors", "sleep")[1].strip(), "Sensors asleep.")
        self.assertEqual(self.ctl("sensors", "wake")[1].strip(), "Sensors awake.")
        code, _, err = self.ctl("rumble", "99")
        self.assertEqual((code, "between" in err), (1, True))
        self.stop()

    def test_commands_that_need_a_remote_explain_themselves(self):
        self.start()
        for argv in (["calibrate"], ["rumble"], ["led", "3"], ["sensors", "sleep"]):
            code, _, err = self.ctl(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("no Wii Remote is connected", err)

    def test_devices(self):
        self.connected("/g1")
        out = self.ctl("devices")[1]
        self.assertIn("AA", out)
        self.assertIn("gyro", out)
        self.stop()

    def test_raw(self):
        self.start()
        out = self.ctl("raw", "hello")[1]
        self.assertEqual(json.loads(out)["protocol"], 1)
        out = self.ctl("raw", "config.get", '{"key": "pointer.sensitivity"}')[1]
        self.assertEqual(json.loads(out)["value"], 30.0)
        self.assertEqual(self.ctl("raw", "nope")[0], 1)
        self.assertEqual(self.ctl("raw", "hello", "[1]")[0], 1)
        self.assertEqual(self.ctl("raw", "hello", "{oops")[0], 1)


class WatchTests(CliCase):
    def test_watch_streams_state_and_button_events(self):
        buttons, gyro, _ = self.connected("/g1")
        threading.Timer(0.3, lambda: buttons.push(key(E.BTN_SOUTH, 1), key(E.BTN_SOUTH, 0))).start()
        code, out, _ = self.ctl("watch", "--topics", "state", "buttons", "--count", "3", "--timeout", "3")
        lines = out.strip().splitlines()
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("state    connected=True mode=calibrating"))
        self.assertEqual(lines[1:], ["button   a down", "button   a up"])
        self.stop()

    def test_watch_json_lines(self):
        buttons, gyro, _ = self.connected("/g1")
        threading.Timer(0.3, lambda: buttons.push(key(E.KEY_UP, 1))).start()
        code, out, _ = self.ctl("--json", "watch", "--topics", "buttons", "--count", "1", "--timeout", "3")
        self.assertEqual(json.loads(out), {"event": "buttons", "data": {"button": "up", "pressed": True}})
        self.stop()

    def test_watch_stops_after_the_timeout(self):
        self.start()
        code, out, _ = self.ctl("watch", "--topics", "buttons", "--timeout", "0.2")
        self.assertEqual((code, out), (0, ""))

    def test_event_descriptions(self):
        self.assertIn("yaw=1", ctl.describe_event("motion", {"yaw": 1, "pitch": 2, "dx": 3, "dy": 4,
                                                             "roll": None, "calibrating": False}))
        self.assertEqual(ctl.describe_event("ext", {"device": "nunchuk", "values": {"BTN_C": 1, "ABS_HAT0X": 5}}),
                         "ext      nunchuk ABS_HAT0X=5 BTN_C=1")
        self.assertEqual(ctl.duration(5), "5s")
        self.assertEqual(ctl.duration(125), "2m 05s")
        self.assertEqual(ctl.duration(7260), "2h 01m")


class UiCommandTests(unittest.TestCase):
    def test_missing_gui_libraries_give_a_helpful_message(self):
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"wiimote_mouse.ui.app": None}), contextlib.redirect_stderr(err):
            code = ctl.main(["ui"])
        self.assertEqual(code, 1)
        self.assertIn("cannot start", err.getvalue())
        self.assertIn("pacman -S python-gobject", err.getvalue())

    def test_the_socket_option_is_passed_to_the_ui(self):
        seen = []
        fake = types.SimpleNamespace(main=lambda argv: seen.append(argv) or 0)
        with mock.patch.dict(sys.modules, {"wiimote_mouse.ui.app": fake}):
            self.assertEqual(ctl.main(["--socket", "/tmp/x.sock", "ui"]), 0)
            self.assertEqual(ctl.main(["ui"]), 0)
        self.assertEqual(seen, [["--socket", "/tmp/x.sock"], []])


class UiLauncherTests(unittest.TestCase):
    def test_launcher_reports_missing_libraries_without_a_traceback(self):
        from wiimote_mouse.ui import launch
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"wiimote_mouse.ui.app": None}), contextlib.redirect_stderr(err):
            self.assertEqual(launch.main([]), 1)
        self.assertIn("wiimote-mouse-ui: cannot start", err.getvalue())
        self.assertIn("python-gobject", err.getvalue())

    def test_launcher_hands_over_to_the_app(self):
        from wiimote_mouse.ui import launch
        fake = types.SimpleNamespace(main=lambda argv: 7)
        with mock.patch.dict(sys.modules, {"wiimote_mouse.ui.app": fake}):
            self.assertEqual(launch.main(["--socket", "x"]), 7)


class DoctorCommandTests(unittest.TestCase):
    def test_doctor_runs_locally_without_a_daemon(self):
        checks = [doctor.Check(doctor.OK, "fine"), doctor.Check(doctor.WARN, "hmm", "try this")]
        out = io.StringIO()
        with mock.patch.object(doctor, "collect", return_value=checks), contextlib.redirect_stdout(out):
            code = ctl.main(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("✓ fine", out.getvalue())
        self.assertIn("→ try this", out.getvalue())


if __name__ == "__main__":
    unittest.main()
