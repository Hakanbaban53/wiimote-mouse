"""The daemon's life with a remote: connecting, disconnecting, buttons, profiles, power."""

import errno
import json
import math
import time
import unittest
from unittest import mock

from evdev import ecodes as E

from wiimote_mouse import dsp, feedback, settings
from tests.helpers import (DaemonCase, FakeLeds, FakeNotifier, FakeRumble, ev, key, stationary_report,
                           wait_for)


class LifecycleTests(DaemonCase):
    def test_waits_for_remote_then_clicks(self):
        self.start()
        time.sleep(0.15)
        self.assertTrue(self.thread.is_alive())
        buttons, _, _ = self.world.add_remote("AA", "/b1")
        self.assertTrue(wait_for(lambda: buttons.grabbed))
        buttons.push(key(E.BTN_1, 1), key(E.BTN_1, 0))
        self.assertTrue(wait_for(lambda: self.mouse.count("button") == 2))
        self.assertEqual(self.mouse.log[:2], [("button", E.BTN_LEFT, True), ("button", E.BTN_LEFT, False)])
        self.stop()
        self.assertTrue(buttons.closed)

    def test_gyro_moves_cursor_and_late_motionplus_is_picked_up(self):
        buttons, _, _ = self.world.add_remote("AA", "/b1")
        self.start()
        self.assertTrue(wait_for(lambda: buttons.grabbed))
        buttons.push(key(E.KEY_UP, 1), key(E.KEY_UP, 0))
        self.assertTrue(wait_for(lambda: self.mouse.count("scroll") == 1))
        gyro = self.world.add_gyro("AA", "/g1")
        self.calibrate(gyro)
        buttons.push(key(E.BTN_EAST, 1))
        time.sleep(0.05)
        t0 = 6000.0
        for i in range(60):
            gyro.push(ev(E.EV_ABS, dsp.X_AXIS, 101.0 + 20 * settings.GYRO_UNITS_PER_DEGREE_S, t0 + i * 0.01),
                      ev(E.EV_ABS, dsp.Y_AXIS, -50.0, t0 + i * 0.01),
                      ev(E.EV_SYN, E.SYN_REPORT, 0, t0 + i * 0.01))
            time.sleep(0.001)
        self.assertTrue(wait_for(lambda: self.mouse.count("emit") > 10))
        self.stop()

    def test_disconnect_releases_buttons_and_reconnect_works(self):
        buttons, gyro, _ = self.connected("/g1")
        buttons.push(key(E.BTN_1, 1))
        self.assertTrue(wait_for(lambda: self.mouse.count("button") == 1))
        settings.set_override("pointer.sensitivity", 40.0)
        buttons.kill()
        gyro.kill()
        self.world.remove_remote("AA")
        self.assertTrue(wait_for(lambda: self.mouse.count("release_all") >= 1))
        self.assertTrue(wait_for(lambda: buttons.closed and gyro.closed))
        self.assertTrue(self.thread.is_alive(), "the daemon must survive a disconnect")
        self.assertTrue(self.controller.calibrating)
        self.assertFalse(self.controller.active)
        self.assertIsNone(self.daemon.session)
        buttons2, _, _ = self.world.add_remote("AA", "/b2", "/g2")
        self.assertTrue(wait_for(lambda: buttons2.grabbed))
        buttons2.push(key(E.BTN_2, 1))
        self.assertTrue(wait_for(lambda: ("button", E.BTN_RIGHT, True) in self.mouse.log))
        self.assertEqual(settings.SENSITIVITY, 40.0)                # a setting, so it survives
        self.stop()

    def test_motionplus_vanishing_keeps_buttons_alive(self):
        buttons, gyro, _ = self.connected("/g1")
        self.calibrate(gyro)
        gyro.kill()
        self.assertTrue(wait_for(lambda: self.controller.calibrating))
        self.assertTrue(wait_for(lambda: gyro.closed))
        buttons.push(key(E.BTN_1, 1))
        self.assertTrue(wait_for(lambda: self.mouse.count("button") == 1))
        self.assertTrue(self.thread.is_alive())
        self.stop()

    def test_grab_busy_is_retried_and_logged_once(self):
        buttons, _, _ = self.world.add_remote("AA", "/b1")
        buttons.grab_error = OSError(errno.EBUSY, "busy")
        with self.assertLogs("wiimote-mouse", level="WARNING") as logs:
            self.start()
            time.sleep(0.3)
            self.assertTrue(self.thread.is_alive())
            buttons.grab_error = None
            self.assertTrue(wait_for(lambda: buttons.grabbed))
            self.stop()
        busy = [m for m in logs.output if "in use by another program" in m]
        self.assertEqual(len(busy), 1, "the same problem must be logged once, not on every retry")

    def test_syn_dropped_releases_stuck_inputs(self):
        buttons, _, _ = self.connected("/g1")
        buttons.push(key(E.BTN_1, 1), key(E.KEY_UP, 1))
        self.assertTrue(wait_for(lambda: ("button", E.BTN_LEFT, True) in self.mouse.log))
        buttons.active = set()
        buttons.push(ev(E.EV_SYN, E.SYN_DROPPED))
        self.assertTrue(wait_for(lambda: ("button", E.BTN_LEFT, False) in self.mouse.log))
        scrolls = self.mouse.count("scroll")
        time.sleep(0.6)
        self.assertEqual(self.mouse.count("scroll"), scrolls, "the D-pad must not keep repeating")
        self.stop()

    def test_no_timer_wakeups_while_idle(self):
        settings.IDLE_SLEEP_SECONDS = 0
        buttons, gyro, _ = self.connected("/g1")
        time.sleep(0.1)
        self.select_timeouts.clear()
        time.sleep(0.5)
        self.assertEqual(self.select_timeouts, [])
        self.stop()

    def test_shutdown_wakes_blocked_select_quickly(self):
        buttons, gyro, _ = self.connected("/g1")
        t = time.monotonic()
        self.stop()
        self.assertLess(time.monotonic() - t, 0.5)
        self.assertFalse(buttons.grabbed)

    def test_accelerometer_is_not_opened_unless_wanted(self):
        buttons, gyro, accel = self.connected("/g1", "/a1")
        time.sleep(0.15)
        self.assertEqual(accel.open_count, 0)
        self.stop()

    def test_roll_compensation_feeds_the_estimator(self):
        settings.ROLL_COMPENSATION = True
        buttons, gyro, accel = self.connected("/g1", "/a1")
        self.assertTrue(wait_for(lambda: accel.open_count == 1))
        a = math.radians(30)
        for i in range(30):
            ts = 7000.0 + i * 0.01
            accel.push(ev(E.EV_ABS, E.ABS_RX, 100 * math.sin(a), ts), ev(E.EV_ABS, E.ABS_RY, 0, ts),
                       ev(E.EV_ABS, E.ABS_RZ, 100 * math.cos(a), ts), ev(E.EV_SYN, E.SYN_REPORT, 0, ts))
            time.sleep(0.001)
        self.assertTrue(wait_for(lambda: abs(math.degrees(self.controller.roll.angle) - 30) < 1))
        self.stop()

    def test_turning_roll_compensation_off_closes_the_accelerometer(self):
        settings.ROLL_COMPENSATION = True
        buttons, gyro, accel = self.connected("/g1", "/a1")
        self.assertTrue(wait_for(lambda: accel.open_count == 1))
        settings.ROLL_COMPENSATION = False
        self.daemon.session.next_scan = 0.0
        self.daemon.wakeup.notify()
        self.assertTrue(wait_for(lambda: accel.closed))
        self.stop()

    def test_a_second_remote_is_ignored_with_a_warning(self):
        self.world.add_remote("AA", "/b1")
        self.world.add_remote("BB", "/b2")
        with self.assertLogs("wiimote-mouse", level="WARNING") as logs:
            self.start()
            self.assertTrue(wait_for(lambda: self.world.devices["/b1"].grabbed))
            self.stop()
        self.assertTrue(any("More than one" in m for m in logs.output))
        self.assertFalse(self.world.devices["/b2"].grabbed)


class ProfileEngineTests(DaemonCase):
    def press(self, buttons, code, down=True):
        buttons.push(key(code, 1 if down else 0))

    def test_default_profile_maps_the_classic_buttons(self):
        buttons, _, _ = self.connected("/g1")
        self.press(buttons, E.BTN_2)
        self.press(buttons, E.BTN_2, False)
        self.assertTrue(wait_for(lambda: self.mouse.log[:2] == [("button", E.BTN_RIGHT, True),
                                                                ("button", E.BTN_RIGHT, False)]))
        self.press(buttons, E.BTN_MODE)
        self.assertTrue(wait_for(lambda: self.controller.calibrating))
        self.stop()

    def test_presentation_profile_sends_keys_instead_of_scrolling(self):
        buttons, _, _ = self.connected("/g1")
        self.daemon.set_profile("presentation")
        self.assertEqual(self.keyboards_made, 1)
        self.press(buttons, E.KEY_RIGHT)
        self.assertTrue(wait_for(lambda: self.keyboard.held == {E.KEY_RIGHT}))
        self.press(buttons, E.KEY_RIGHT, False)
        self.assertTrue(wait_for(lambda: not self.keyboard.held))
        self.assertEqual(self.keyboard.log, [("press", (E.KEY_RIGHT,)), ("release", (E.KEY_RIGHT,))])
        self.assertEqual(self.mouse.count("scroll"), 0)
        self.stop()

    def test_release_follows_the_binding_that_was_pressed(self):
        buttons, _, _ = self.connected("/g1")
        self.daemon.set_profile("presentation")
        self.press(buttons, E.BTN_SOUTH)                        # A = KEY_RIGHT in presentation
        self.assertTrue(wait_for(lambda: self.keyboard.held == {E.KEY_RIGHT}))
        self.daemon.set_profile("default")                      # A is scroll.hold there
        self.press(buttons, E.BTN_SOUTH, False)
        self.assertTrue(wait_for(lambda: not self.keyboard.held), "the key must not stay pressed")
        self.assertFalse(self.controller.a_held)
        self.stop()

    def test_chords_middle_button_and_none(self):
        self.configure('[profiles.t.buttons]\none = "key:KEY_LEFTCTRL+KEY_C"\ntwo = "mouse.middle"\n'
                       'a = "none"\n')
        self.daemon.set_profile("t")
        buttons, _, _ = self.connected("/g1")
        self.press(buttons, E.BTN_1)
        self.press(buttons, E.BTN_1, False)
        self.press(buttons, E.BTN_2)
        self.press(buttons, E.BTN_SOUTH)
        self.assertTrue(wait_for(lambda: ("button", E.BTN_MIDDLE, True) in self.mouse.log))
        self.assertEqual(self.keyboard.log[:2], [("press", (E.KEY_LEFTCTRL, E.KEY_C)),
                                                 ("release", (E.KEY_LEFTCTRL, E.KEY_C))])
        self.assertFalse(self.controller.a_held)
        self.stop()

    def test_exec_actions_run_in_order_without_a_shell(self):
        self.configure('[profiles.mic.buttons]\n'
                       'two = { press = "exec:wpctl set-mute SRC 0", release = "exec:wpctl set-mute SRC 1" }\n'
                       'one = "exec:notify-send hello"\n')
        self.daemon.set_profile("mic")
        buttons, _, _ = self.connected("/g1")
        self.press(buttons, E.BTN_2)
        self.press(buttons, E.BTN_2, False)
        self.press(buttons, E.BTN_1)
        self.assertTrue(wait_for(lambda: len(self.runner.commands) == 3))
        self.assertEqual(self.runner.commands, [["wpctl", "set-mute", "SRC", "0"],
                                                ["wpctl", "set-mute", "SRC", "1"],
                                                ["notify-send", "hello"]])
        self.stop()

    def test_disconnect_while_holding_runs_the_release_actions(self):
        self.configure('[profiles.mic.buttons]\ntwo = { press = "exec:mute off", release = "exec:mute on" }\n'
                       'one = "key:KEY_F13"\n')
        self.daemon.set_profile("mic")
        buttons, gyro, _ = self.connected("/g1")
        self.press(buttons, E.BTN_2)
        self.press(buttons, E.BTN_1)
        self.assertTrue(wait_for(lambda: self.keyboard.held == {E.KEY_F13}))
        buttons.kill()
        gyro.kill()
        self.world.remove_remote("AA")
        self.assertTrue(wait_for(lambda: not self.keyboard.held), "a held key must be let go")
        self.assertTrue(wait_for(lambda: self.runner.commands[-1] == ["mute", "on"]))
        self.stop()

    def test_syn_dropped_releases_keys(self):
        buttons, _, _ = self.connected("/g1")
        self.daemon.set_profile("media")
        self.press(buttons, E.KEY_UP)
        self.assertTrue(wait_for(lambda: self.keyboard.held == {E.KEY_VOLUMEUP}))
        buttons.active = set()
        buttons.push(ev(E.EV_SYN, E.SYN_DROPPED))
        self.assertTrue(wait_for(lambda: not self.keyboard.held))
        self.stop()

    def test_profile_switching_actions(self):
        self.configure('[profiles.t.buttons]\nhome = "profile.previous"\none = "profile:media"\n')
        self.daemon.set_profile("t")
        buttons, _, _ = self.connected("/g1")
        self.press(buttons, E.BTN_MODE)
        self.assertTrue(wait_for(lambda: self.daemon.profiles.active == "media"))
        self.daemon.set_profile("t")
        self.press(buttons, E.BTN_1)
        self.assertTrue(wait_for(lambda: self.daemon.profiles.active == "media"))
        self.stop()

    def test_sensitivity_buttons_change_the_setting_and_persist(self):
        buttons, _, _ = self.connected("/g1")
        self.press(buttons, E.KEY_NEXT)
        self.assertTrue(wait_for(lambda: settings.SENSITIVITY > 30))
        self.assertAlmostEqual(settings.SENSITIVITY, 30 * 1.12, places=3)
        self.assertTrue(wait_for(lambda: self.overrides_path.exists()))
        saved = json.loads(self.overrides_path.read_text())
        self.assertAlmostEqual(saved["settings"]["pointer.sensitivity"]["value"], 33.6, places=2)
        self.stop()

    def test_sensitivity_stops_at_the_limits(self):
        settings.SENSITIVITY = settings.SENSITIVITY_MAX
        buttons, _, _ = self.connected("/g1")
        self.press(buttons, E.KEY_NEXT)
        time.sleep(0.1)
        self.assertEqual(settings.SENSITIVITY, settings.SENSITIVITY_MAX)
        self.stop()

    def test_scroll_actions_repeat_while_held(self):
        buttons, _, _ = self.connected("/g1")
        self.press(buttons, E.KEY_DOWN)
        self.assertTrue(wait_for(lambda: self.mouse.count("scroll") >= 3, timeout=3))
        self.press(buttons, E.KEY_DOWN, False)
        time.sleep(0.2)
        count = self.mouse.count("scroll")
        time.sleep(0.4)
        self.assertEqual(self.mouse.count("scroll"), count)
        self.assertEqual(self.mouse.log[0], ("scroll", E.REL_WHEEL, -1, -120))
        self.stop()

    def test_missing_keyboard_permission_disables_key_actions_only(self):
        self.daemon._keyboard_factory = mock.Mock(side_effect=PermissionError("no uinput"))
        with self.assertLogs("wiimote-mouse", level="WARNING") as logs:
            self.daemon.set_profile("presentation")
            buttons, _, _ = self.connected("/g1")
            self.press(buttons, E.BTN_SOUTH)            # A is a key in presentation: nothing happens
            self.press(buttons, E.BTN_1)                # 1 is still the left mouse button
            self.assertTrue(wait_for(lambda: ("button", E.BTN_LEFT, True) in self.mouse.log))
            self.stop()
        self.assertEqual(sum("virtual keyboard" in m for m in logs.output), 1)
        self.daemon._keyboard_factory.assert_called_once()

    def test_active_profile_is_restored_from_the_saved_state(self):
        self.state.set("profile", "media")
        self.state.flush()
        self.daemon.restore_profile()
        self.assertEqual(self.daemon.profiles.active, "media")
        self.assertEqual(self.keyboards_made, 1)                # media needs the keyboard
        self.state.set("profile", "gone")
        self.daemon.profiles.activate("default")
        self.daemon.restore_profile()
        self.assertEqual(self.daemon.profiles.active, "default")


class PowerAndFeedbackTests(DaemonCase):
    def setUp(self):
        super().setUp()
        self.leds, self.rumble, self.notifier = FakeLeds(), FakeRumble(), FakeNotifier()

        def create(remote, timers, sensitivity):
            return feedback.Feedback(timers, sensitivity, self.leds, self.rumble, self.notifier)

        patcher = mock.patch.object(feedback.Feedback, "create", staticmethod(create))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_led_and_rumble_follow_the_remote_state(self):
        buttons, gyro, _ = self.connected("/g1")
        self.assertTrue(wait_for(lambda: self.leds.masks[:1] == [0b0110]))
        self.calibrate(gyro)
        self.assertTrue(wait_for(lambda: self.leds.masks[-1] == 0b0111))
        self.assertTrue(wait_for(lambda: self.rumble.pulses == 1))
        for _ in range(3):
            buttons.push(key(E.KEY_NEXT, 1), key(E.KEY_NEXT, 0))
            time.sleep(0.02)
        self.assertTrue(wait_for(lambda: self.leds.masks[-1] == 0b1111))
        self.stop()
        self.assertEqual(self.leds.masks[-1], 0b0001)
        self.assertTrue(self.rumble.closed)

    def test_idle_sleep_closes_sensors_and_any_button_wakes_them(self):
        settings.IDLE_SLEEP_SECONDS = 0.3
        buttons, gyro, _ = self.connected("/g1")
        self.calibrate(gyro)
        settings.IDLE_SLEEP_SECONDS = 0.3                       # configure() isn't called, but be explicit
        self.assertTrue(wait_for(lambda: gyro.closed, timeout=3))
        self.assertTrue(wait_for(lambda: self.leds.masks[-1] == 0b0001))
        self.assertTrue(self.controller.calibrating)
        time.sleep(0.2)
        self.assertEqual(gyro.open_count, 1, "must not reopen on its own while asleep")
        self.select_timeouts.clear()
        time.sleep(0.4)
        self.assertEqual(self.select_timeouts, [], "a sleeping remote needs zero wakeups")
        buttons.push(key(E.BTN_2, 1), key(E.BTN_2, 0))
        self.assertTrue(wait_for(lambda: gyro.open_count == 2))
        self.assertTrue(wait_for(lambda: self.leds.masks[-1] == 0b0110))
        self.stop()

    def test_holding_the_clutch_counts_as_activity(self):
        settings.IDLE_SLEEP_SECONDS = 0.25
        buttons, gyro, _ = self.connected("/g1")
        self.calibrate(gyro)
        buttons.push(key(E.BTN_EAST, 1))
        for i in range(30):
            gyro.push(*stationary_report(9000.0 + i * 0.01))
            time.sleep(0.015)
        self.assertFalse(gyro.closed)
        self.stop()

    def test_low_battery_reaches_notification_and_rumble(self):
        from wiimote_mouse import devices
        with mock.patch.object(devices, "find_battery", lambda key: "/fake/capacity"), \
                mock.patch.object(devices, "read_battery", lambda path: 12.0):
            settings.BATTERY_FIRST_POLL_DELAY = 0.05
            self.connected("/g1")
            self.assertTrue(wait_for(lambda: self.notifier.sent == [("Wii Remote battery low", False)]))
            self.assertTrue(wait_for(lambda: self.rumble.pulses == 3))
            self.stop()


if __name__ == "__main__":
    unittest.main()
