"""The GTK UI against a live daemon. Needs a display; skipped without one.

    xvfb-run -a python3 -m unittest tests.test_ui_app
"""

import math
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("GSK_RENDERER", "cairo")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4, libadwaita and PyGObject are not installed")

from evdev import ecodes as E  # noqa: E402

from wiimote_mouse import dsp, doctor, settings  # noqa: E402
from tests.helpers import DaemonCase, ev, key  # noqa: E402

Gtk.init_check()
HAVE_DISPLAY = Gdk.Display.get_default() is not None     # init_check() alone says True even without one
_APP = None


def app():
    global _APP
    if _APP is None:
        _APP = Adw.Application(application_id="dev.wiimotemouse.Tests", flags=Gio.ApplicationFlags.NON_UNIQUE)
        _APP.register(None)
    return _APP


def pump(predicate=None, timeout=3.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while context.iteration(False):
            pass
        if predicate is not None and predicate():
            return True
        time.sleep(0.005)
    return predicate() if predicate is not None else True


@unittest.skipUnless(HAVE_DISPLAY, "needs a display (try: xvfb-run -a python3 -m unittest tests.test_ui_app)")
class UiCase(DaemonCase):
    serve = True

    def setUp(self):
        super().setUp()
        settings.IDLE_SLEEP_SECONDS = 0
        from wiimote_mouse.ui.app import Window
        self.window = Window(app(), str(self.socket_path))
        self.window.link.retry_ms = 30
        self.window.present()
        self.addCleanup(self._close_window)
        pump(timeout=0.3)

    def _close_window(self):
        self.window.link.stop()
        self.window.destroy()

    def page(self, name):
        self.window.stack.set_visible_child_name(name)
        pump(timeout=0.2)


class ConnectionTests(UiCase):
    def test_shows_the_down_page_until_the_daemon_is_there(self):
        self.assertEqual(self.window.outer.get_visible_child_name(), "down")
        self.start()
        self.assertTrue(pump(lambda: self.window.outer.get_visible_child_name() == "main"))
        self.assertEqual(self.window.remote_page.status_row.get_subtitle(), "Waiting for a Wii Remote")
        self.stop()

    def test_status_follows_the_remote(self):
        buttons, gyro, _ = self.connected("/g1")
        page = self.window.remote_page
        self.assertTrue(pump(lambda: page.status_row.get_subtitle().startswith("Calibrating")))
        self.calibrate(gyro)
        self.assertTrue(pump(lambda: page.status_row.get_subtitle() == "Ready"))
        self.assertTrue(page.calibrate_button.get_sensitive())
        self.assertEqual(page.profile_row.get_subtitle(), "default")
        self.assertEqual(page.speed_row.get_subtitle(), "30")
        self.assertIn("MotionPlus on", page.sensors_row.get_subtitle())
        self.stop()

    def test_losing_the_daemon_shows_the_down_page_and_it_comes_back(self):
        buttons, gyro, _ = self.connected("/g1")
        self.assertTrue(pump(lambda: self.window.outer.get_visible_child_name() == "main"))
        self.daemon.server.close()
        self.daemon.wakeup.notify()                         # the daemon loop rebuilds its fd list
        self.assertTrue(pump(lambda: self.window.outer.get_visible_child_name() == "down"))
        self.assertFalse(self.window.model.connected)
        self.daemon.start_server(self.socket_path)
        self.daemon.wakeup.notify()
        self.assertTrue(pump(lambda: self.window.outer.get_visible_child_name() == "main"))
        self.assertTrue(pump(lambda: self.window.model.connected))
        self.stop()

    def test_buttons_light_up_the_picture(self):
        buttons, gyro, _ = self.connected("/g1")
        page = self.window.remote_page
        self.assertTrue(pump(lambda: self.window.model.connected))
        before = page._remote_key
        buttons.push(key(E.BTN_SOUTH, 1), key(E.KEY_UP, 1))
        self.assertTrue(pump(lambda: self.window.model.pressed == {"a", "up"}))
        self.assertNotEqual(page._remote_key, before)
        buttons.push(key(E.BTN_SOUTH, 0), key(E.KEY_UP, 0))
        self.assertTrue(pump(lambda: not self.window.model.pressed))
        self.stop()

    def test_the_calibrate_button_reaches_the_daemon(self):
        buttons, gyro, _ = self.connected("/g1")
        self.calibrate(gyro)
        self.assertTrue(pump(lambda: self.window.model.mode == "ready"))
        self.window.remote_page.calibrate_button.emit("clicked")
        self.assertTrue(pump(lambda: self.controller.calibrating))
        self.stop()

    def test_a_live_view_keeps_the_sensors_awake_even_if_asked_to_sleep(self):
        buttons, gyro, _ = self.connected("/g1")
        self.assertTrue(pump(lambda: self.daemon.server.has_subscribers("motion")))
        self.client().call("sensors", action="sleep")
        self.assertTrue(pump(lambda: gyro.open_count == 2))               # reopened at once for the live view
        self.assertTrue(pump(lambda: not self.window.model.remote["asleep"]))
        self.stop()


class StreamTests(UiCase):
    def test_motion_is_only_requested_while_the_remote_page_is_visible(self):
        buttons, gyro, accel = self.connected("/g1", "/a1")
        self.assertTrue(pump(lambda: self.daemon.server.has_subscribers("motion")))
        self.assertTrue(pump(lambda: accel.open_count == 1))
        self.page("settings")
        self.assertTrue(pump(lambda: not self.daemon.server.has_subscribers("motion")))
        self.assertTrue(pump(lambda: accel.closed))
        self.page("remote")
        self.assertTrue(pump(lambda: self.daemon.server.has_subscribers("motion")))
        self.assertTrue(self.daemon.server.has_subscribers("buttons"))
        self.stop()

    def test_the_gauges_move_with_the_remote(self):
        buttons, gyro, accel = self.connected("/g1", "/a1")
        self.calibrate(gyro)
        self.assertTrue(pump(lambda: accel.open_count == 1))
        angle = math.radians(30)
        for i in range(30):
            ts = 7000.0 + i * 0.01
            accel.push(ev(E.EV_ABS, E.ABS_RX, 100 * math.sin(angle), ts), ev(E.EV_ABS, E.ABS_RY, 0, ts),
                       ev(E.EV_ABS, E.ABS_RZ, 100 * math.cos(angle), ts), ev(E.EV_SYN, E.SYN_REPORT, 0, ts))
            gyro.push(ev(E.EV_ABS, dsp.X_AXIS, self.controller.center[dsp.X_AXIS] + 25 * settings.GYRO_UNITS_PER_DEGREE_S, ts),
                      ev(E.EV_ABS, dsp.Y_AXIS, self.controller.center[dsp.Y_AXIS], ts), ev(E.EV_SYN, E.SYN_REPORT, 0, ts))
            pump(timeout=0.005)
        self.assertTrue(pump(lambda: self.window.model.motion and self.window.model.motion["roll"] is not None))
        self.assertAlmostEqual(self.window.model.motion["roll"], 30, delta=2)
        self.assertIsNotNone(self.window.remote_page._gauge_key[0])
        self.stop()

    def test_extensions_get_a_panel_that_follows_their_input(self):
        buttons, gyro, _ = self.connected("/g1", nunchuk="/n1")
        page = self.window.remote_page
        self.assertTrue(pump(lambda: page.ext_group.get_visible()))
        self.assertIn("nunchuk", page._ext_rows)
        self.assertTrue(pump(lambda: self.daemon.server.has_subscribers("ext")))
        nunchuk = self.world.devices["/n1"]
        self.assertTrue(pump(lambda: nunchuk.open_count == 1))
        nunchuk.push(ev(E.EV_ABS, E.ABS_HAT0X, 80), ev(E.EV_KEY, E.BTN_Z, 1), ev(E.EV_SYN, E.SYN_REPORT, 0))
        self.assertTrue(pump(lambda: self.window.model.ext.get("nunchuk", {}).get("BTN_Z") == 1))
        self.assertIsNotNone(page._nunchuk_key)
        self.world.remove_extension("AA", "nunchuk")
        self.assertTrue(pump(lambda: not page.ext_group.get_visible()))
        self.stop()


class SettingsPageTests(UiCase):
    def open_settings(self):
        self.start()
        self.assertTrue(pump(lambda: self.window.settings_page.rows))
        self.page("settings")

    def test_there_is_a_row_for_every_setting_with_its_current_value(self):
        self.open_settings()
        rows = self.window.settings_page.rows
        self.assertEqual(set(rows), {o.key for o in settings.OPTIONS})
        self.assertEqual(rows["pointer.sensitivity"][0].get_value(), 30.0)
        self.assertTrue(rows["pointer.invert_x"][0].get_active())
        self.assertFalse(rows["pointer.sensitivity"][1].get_visible())            # nothing to reset yet

    def test_changing_a_row_changes_the_daemon(self):
        self.open_settings()
        rows = self.window.settings_page.rows
        rows["pointer.sensitivity"][0].set_value(44)
        self.assertTrue(pump(lambda: settings.SENSITIVITY == 44.0))
        rows["pointer.invert_y"][0].set_active(True)
        self.assertTrue(pump(lambda: settings.INVERT_Y is True))
        self.assertTrue(pump(lambda: rows["pointer.sensitivity"][1].get_visible()))
        self.assertIn("Changed (override)", rows["pointer.sensitivity"][0].get_subtitle())
        rows["pointer.sensitivity"][1].emit("clicked")                          # the reset button
        self.assertTrue(pump(lambda: settings.SENSITIVITY == 30.0))
        self.assertTrue(pump(lambda: rows["pointer.sensitivity"][0].get_value() == 30.0))
        self.stop()

    def test_rows_follow_changes_made_elsewhere(self):
        self.open_settings()
        client = self.client()
        client.call("config.set", key="pointer.sensitivity", value=55)
        self.assertTrue(pump(lambda: self.window.settings_page.rows["pointer.sensitivity"][0].get_value() == 55.0))
        self.assertEqual(settings.SENSITIVITY, 55.0)        # and the UI did not echo a second change back

    def test_a_setting_that_needs_a_restart_says_so(self):
        self.open_settings()
        self.assertIn("restart", self.window.settings_page.rows["scroll.hires"][0].get_subtitle())


class ProfilesPageTests(UiCase):
    def open_profiles(self):
        self.start()
        self.assertTrue(pump(lambda: self.window.profiles_page.names))
        self.page("profiles")

    def test_lists_profiles_and_buttons(self):
        self.open_profiles()
        page = self.window.profiles_page
        self.assertEqual(page.names, ["default", "presentation", "media"])
        self.assertEqual(page.entries["a"].get_text(), "scroll.hold")
        self.assertFalse(page.delete_button.get_sensitive())

    def test_choosing_a_profile_activates_it(self):
        self.open_profiles()
        page = self.window.profiles_page
        page.combo.set_selected(page.names.index("presentation"))
        self.assertTrue(pump(lambda: self.daemon.profiles.active == "presentation"))
        self.assertTrue(pump(lambda: page.entries["right"].get_text() == "key:KEY_RIGHT"))
        self.assertIn("(inherited)", page.entries["b"].get_title())
        self.assertNotIn("(inherited)", page.entries["right"].get_title())

    def test_editing_a_button_saves_it_and_clearing_goes_back(self):
        self.open_profiles()
        page = self.window.profiles_page
        page.entries["two"].set_text("key:KEY_F13")
        page.entries["two"].emit("apply")
        self.assertTrue(pump(lambda: "default" in settings.ui_profiles()))
        self.assertEqual(settings.ui_profiles()["default"]["buttons"], {"two": "key:KEY_F13"})
        self.assertTrue(pump(lambda: page.delete_button.get_sensitive()))
        self.assertEqual(page.delete_button.get_label(), "Reset my changes")
        page.entries["two"].set_text("")
        page.entries["two"].emit("apply")
        self.assertTrue(pump(lambda: not settings.ui_profiles().get("default", {}).get("buttons")))
        self.assertTrue(pump(lambda: page.entries["two"].get_text() == "mouse.right"))

    def test_table_form_actions_round_trip_as_json(self):
        self.open_profiles()
        page = self.window.profiles_page
        page.entries["one"].set_text('{"press": "exec:echo on", "release": "exec:echo off"}')
        page.entries["one"].emit("apply")
        self.assertTrue(pump(lambda: "default" in settings.ui_profiles()))
        self.assertEqual(settings.ui_profiles()["default"]["buttons"]["one"],
                         {"press": "exec:echo on", "release": "exec:echo off"})
        self.assertTrue(pump(lambda: page.entries["one"].get_text().startswith("{")))

    def test_invalid_input_is_reported_and_nothing_is_saved(self):
        self.open_profiles()
        page = self.window.profiles_page
        toasts = []
        with mock.patch.object(type(self.window), "toast", lambda w, text: toasts.append(text)):
            page.entries["a"].set_text("fly")
            page.entries["a"].emit("apply")
            self.assertTrue(pump(lambda: toasts))
            page.entries["a"].set_text("{not json")
            page.entries["a"].emit("apply")
            self.assertTrue(pump(lambda: len(toasts) == 2))
        self.assertIn("unknown action", toasts[0])
        self.assertIn("not valid JSON", toasts[1])
        self.assertEqual(settings.ui_profiles(), {})
        self.assertEqual(page.entries["a"].get_text(), "{not json")            # kept, so it can be fixed
        self.assertTrue(page.entries["a"].has_css_class("error"))
        page.entries["a"].set_text("calibrate")                                # typing clears the error mark
        self.assertFalse(page.entries["a"].has_css_class("error"))

    def test_a_saved_profile_can_be_deleted(self):
        self.open_profiles()
        page = self.window.profiles_page
        self.window.command("profile.save", None, name="mine", profile={"buttons": {"a": "calibrate"}})
        self.window.command("profile.set", lambda *_: page.refresh(), name="mine")
        self.assertTrue(pump(lambda: page.shown == "mine" and page.delete_button.get_sensitive()))
        self.assertEqual(page.delete_button.get_label(), "Delete profile")
        page.delete_button.emit("clicked")
        self.assertTrue(pump(lambda: "mine" not in page.names))
        self.assertEqual(self.daemon.profiles.active, "default")


class DiagnosticsPageTests(UiCase):
    def test_runs_the_checks_when_opened(self):
        checks = [doctor.Check(doctor.OK, "all good"), doctor.Check(doctor.WARN, "careful <b>", "do this"),
                  doctor.Check(doctor.FAIL, "broken", "fix it")]
        with mock.patch.object(doctor, "collect", return_value=checks):
            self.start()
            self.page("diagnostics")
            page = self.window.diagnostics_page
            self.assertTrue(pump(lambda: len(page.rows) == 3))
        self.assertEqual(page.rows[1].get_title(), "careful <b>")           # shown as text, not as markup
        self.assertEqual(page.rows[2].get_subtitle(), "fix it")
        self.assertTrue(page.button.get_sensitive())


if __name__ == "__main__":
    unittest.main()
