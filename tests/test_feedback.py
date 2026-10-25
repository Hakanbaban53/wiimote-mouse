import errno
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from evdev import ecodes as E

from wiimote_mouse import devices, feedback, runtime, settings
from tests.helpers import FakeDevice, FakeLeds, FakeNotifier, FakeRumble, SettingsIsolation, temp_dir, wait_for


class LedBarTests(unittest.TestCase):
    def setUp(self):
        directory = temp_dir(self)
        self.paths = []
        for i in range(4):
            path = os.path.join(directory, f"p{i}")
            Path(path).write_text("0")
            self.paths.append(path)

    def states(self):
        return [Path(p).read_text() for p in self.paths]

    def test_masks_and_minimal_writes(self):
        bar = feedback.LedBar(self.paths)
        bar.set_mask(0b0110)
        self.assertEqual(self.states(), ["0", "1", "1", "0"])
        bar.set_mask(0b0111)
        self.assertEqual(self.states(), ["1", "1", "1", "0"])
        with mock.patch("builtins.open", side_effect=AssertionError("no write expected")):
            bar.set_mask(0b0111)

    def test_permission_error_disables_leds_quietly(self):
        bar = feedback.LedBar(self.paths)
        real_open = open

        def deny(path, mode="r", *a, **k):
            if "w" in mode:
                raise PermissionError(errno.EACCES, "denied")
            return real_open(path, mode, *a, **k)

        with mock.patch("builtins.open", deny):
            bar.set_mask(0b1111)
        self.assertFalse(bar.enabled)
        bar.set_mask(0b0001)


class RumbleTests(unittest.TestCase):
    def test_uploads_once_and_pulses(self):
        dev = FakeDevice()
        rumble = feedback.Rumble(dev)
        self.assertTrue(rumble.available)
        rumble.pulse()
        rumble.pulse()
        self.assertEqual(dev.ff_plays, [(E.EV_FF, 7, 1)] * 2)
        rumble.close()
        self.assertEqual(dev.erased, [7])

    def test_device_without_rumble_or_write_access_is_tolerated(self):
        class NoFF(FakeDevice):
            def capabilities(self, *a, **k):
                return {}

        class ReadOnly(FakeDevice):
            def upload_effect(self, effect):
                raise PermissionError(errno.EACCES, "read-only fd")

        self.assertFalse(feedback.Rumble(NoFF()).available)
        self.assertFalse(feedback.Rumble(ReadOnly()).available)

    def test_failing_pulse_disables_rumble(self):
        class Dying(FakeDevice):
            def write(self, *args):
                raise OSError(errno.ENODEV, "gone")

        rumble = feedback.Rumble(Dying())
        rumble.pulse()
        self.assertFalse(rumble.available)


class NotifierTests(unittest.TestCase):
    def test_sends_in_background_and_respects_enabled(self):
        sent = []
        done = threading.Event()

        def runner(command):
            sent.append(command)
            done.set()

        feedback.Notifier(True, runner).send("t", "b", urgent=True)
        self.assertTrue(done.wait(2))
        self.assertEqual(sent[0][-2:], ["t", "b"])
        self.assertIn("critical", sent[0])
        feedback.Notifier(False, runner).send("x", "y")
        time.sleep(0.05)
        self.assertEqual(len(sent), 1)


class BatteryMonitorTests(unittest.TestCase):
    def test_reads_in_a_worker_and_wakes_the_loop(self):
        woke = threading.Event()
        values = iter([55.0, 40.0])

        def read():
            try:
                return next(values)
            except StopIteration:
                raise OSError("done")

        monitor = feedback.BatteryMonitor(read, woke.set, interval=0.05, first_delay=0.02)
        monitor.start()
        self.assertTrue(woke.wait(2))
        self.assertTrue(wait_for(lambda: len(monitor._results) == 2))
        monitor.stop()
        self.assertEqual(monitor.poll(), [55.0, 40.0])
        self.assertEqual(monitor.poll(), [])

    def test_slow_read_never_blocks_the_caller(self):
        release = threading.Event()
        monitor = feedback.BatteryMonitor(lambda: release.wait(5) or 50.0, lambda: None, 10, 0.0)
        monitor.start()
        started = time.monotonic()
        time.sleep(0.05)
        monitor.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        release.set()


class FeedbackTests(SettingsIsolation):
    def make(self, sensitivity=30.0):
        self.timers = runtime.Timers()
        self.leds, self.rumble, self.notifier = FakeLeds(), FakeRumble(), FakeNotifier()
        fb = feedback.Feedback(self.timers, sensitivity, self.leds, self.rumble, self.notifier)
        self.changes = 0

        def changed():
            self.changes += 1

        fb.on_change = changed
        return fb

    def run_timers(self):
        self.timers.run_due(time.monotonic() + 100)

    def test_sensitivity_levels(self):
        self.assertEqual([feedback.sensitivity_level(v) for v in (5, 11.9, 12, 21.9, 22, 39.9, 40, 80)],
                         [1, 1, 2, 2, 3, 3, 4, 4])

    def test_led_states_and_ready_buzz(self):
        fb = self.make(30.0)
        fb.waiting()
        fb.calibrating()
        fb.ready()
        fb.asleep()
        self.assertEqual(self.leds.masks, [0b0001, 0b0110, 0b0111, 0b0001])
        self.assertEqual(self.rumble.pulses, 1)
        self.assertGreaterEqual(self.changes, 4)

    def test_bar_follows_sensitivity_only_while_ready(self):
        fb = self.make(30.0)
        fb.calibrating()
        fb.sensitivity_changed(45.0)
        self.assertEqual(self.leds.masks, [0b0110])
        fb.ready()
        self.assertEqual(self.leds.masks[-1], 0b1111)
        fb.sensitivity_changed(8.0)
        self.assertEqual(self.leds.masks[-1], 0b0001)

    def test_manual_led_test_overrides_and_releases(self):
        fb = self.make()
        fb.ready()
        fb.override_leds(0b1010)
        self.assertEqual(self.leds.masks[-1], 0b1010)
        fb.sensitivity_changed(50.0)                    # automatic updates don't fight the test
        self.assertEqual(self.leds.masks[-1], 0b1010)
        fb.override_leds(None)
        self.assertEqual(self.leds.masks[-1], 0b1111)

    def test_limit_buzz(self):
        fb = self.make()
        fb.sensitivity_changed(80.0, at_limit=True)
        self.assertEqual(self.rumble.pulses, 1)

    def test_buzz_patterns_use_timers(self):
        fb = self.make()
        fb.buzz(3)
        self.assertEqual(self.rumble.pulses, 1)
        self.run_timers()
        self.assertEqual(self.rumble.pulses, 3)

    def test_stuck_calibration_nudges_once_and_is_cancelled_by_ready(self):
        settings.CALIBRATION_NOTICE_AFTER = 0.0
        fb = self.make()
        fb.calibrating()
        self.run_timers()
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.rumble.pulses, 2)
        fb = self.make()
        fb.calibrating()
        fb.ready()
        before = self.rumble.pulses
        self.run_timers()
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self.rumble.pulses, before)

    def test_battery_warnings_fire_once_per_level_with_hysteresis(self):
        fb = self.make()
        fb.battery(80)
        self.assertEqual(fb.battery_percent, 80)
        fb.battery(19)
        fb.battery(18)
        self.assertEqual(self.notifier.sent, [("Wii Remote battery low", False)])
        fb.battery(4)
        fb.battery(3)
        self.assertEqual(self.notifier.sent[-1], ("Wii Remote battery almost empty", True))
        self.assertEqual(len(self.notifier.sent), 2)
        fb.battery(90)
        fb.battery(15)
        self.assertEqual(len(self.notifier.sent), 3)

    def test_close_restores_led_one_and_erases_rumble(self):
        fb = self.make()
        fb.ready()
        fb.close()
        self.assertEqual(self.leds.masks[-1], 0b0001)
        self.assertTrue(self.rumble.closed)

    def test_create_respects_settings(self):
        settings.FEEDBACK_LEDS = settings.FEEDBACK_RUMBLE = settings.FEEDBACK_NOTIFY = False
        fb = feedback.Feedback.create(devices.Remote("K", FakeDevice()), runtime.Timers(), 30.0)
        self.assertIsNone(fb.leds)
        self.assertIsNone(fb.rumble)
        self.assertFalse(fb.notifier.enabled)

    def test_reconfigure_follows_the_settings_at_runtime(self):
        settings.FEEDBACK_LEDS = settings.FEEDBACK_RUMBLE = settings.FEEDBACK_NOTIFY = False
        remote = devices.Remote("K", FakeDevice())
        fb = feedback.Feedback.create(remote, runtime.Timers(), 30.0)
        settings.FEEDBACK_RUMBLE = True
        with mock.patch.object(devices, "find_leds", lambda key: ["/x"] * 4), \
                mock.patch.object(feedback, "LedBar", lambda paths: FakeLeds()):
            settings.FEEDBACK_LEDS = True
            fb.reconfigure()
            self.assertIsNotNone(fb.leds)
            self.assertIsNotNone(fb.rumble)
            settings.FEEDBACK_LEDS = settings.FEEDBACK_RUMBLE = False
            fb.reconfigure()
        self.assertIsNone(fb.leds)
        self.assertIsNone(fb.rumble)


if __name__ == "__main__":
    unittest.main()
