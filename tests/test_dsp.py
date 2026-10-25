import math
import unittest

from evdev import ecodes as E

from wiimote_mouse import dsp, settings
from tests.helpers import SettingsIsolation, calibrated_controller, feed


class CoreTests(SettingsIsolation):
    def test_calibration_then_constant_rate_moves_expected_pixels(self):
        c = calibrated_controller(bias=(100.0, -50.0))
        c.switch_mode("B", True)
        rate_dps = 20.0
        raw_x = 100.0 + 1 + rate_dps * settings.GYRO_UNITS_PER_DEGREE_S
        total = feed(c, [(2000.0 + i * 0.01, raw_x, -50.0) for i in range(101)])
        expected = rate_dps * settings.SENSITIVITY * (
            1 + settings.POINTER_ACCELERATION * rate_dps / settings.ACCEL_REFERENCE_DPS)
        self.assertAlmostEqual(abs(total[0]), expected, delta=expected * 0.06)
        self.assertEqual(total[0] < 0, settings.INVERT_X)
        self.assertEqual(total[1], 0)

    def test_sensitivity_is_read_live_from_settings(self):
        c = calibrated_controller()
        c.switch_mode("B", True)
        raw_x = 101.0 + 20 * settings.GYRO_UNITS_PER_DEGREE_S

        def swipe(base):
            c.reset_motion()
            return abs(feed(c, [(base + i * 0.01, raw_x, -50.0) for i in range(101)])[0])

        slow = swipe(2000.0)
        settings.SENSITIVITY = settings.SENSITIVITY * 2
        fast = swipe(3000.0)
        self.assertAlmostEqual(fast / slow, 2.0, delta=0.1)

    def test_fast_flick_saturates_instead_of_freezing(self):
        c = calibrated_controller()
        c.switch_mode("B", True)
        raw_x = 101.0 + 400 * settings.GYRO_UNITS_PER_DEGREE_S
        total = feed(c, [(2000.0 + i * 0.01, raw_x, -50.0) for i in range(21)])
        self.assertGreaterEqual(abs(total[0]), 20 * settings.MAX_PIXELS_PER_FRAME * 0.9)

    def test_physically_impossible_reading_is_still_rejected(self):
        c = calibrated_controller()
        c.switch_mode("B", True)
        feed(c, [(2000.0, 101.0, -50.0), (2000.01, 101.0, -50.0)])
        c.raw[dsp.X_AXIS] = 101.0 + 2500 * settings.GYRO_UNITS_PER_DEGREE_S
        self.assertEqual(c.frame(2000.02), dsp.ZERO)
        self.assertIsNone(c.last_frame)

    def test_stationary_remote_produces_no_motion(self):
        c = calibrated_controller()
        c.switch_mode("B", True)
        self.assertEqual(feed(c, [(2000.0 + i * 0.01, 101.0, -50.0) for i in range(100)]), [0, 0, 0, 0])

    def test_duplicate_timestamp_is_skipped_without_resetting(self):
        c = calibrated_controller()
        c.switch_mode("B", True)
        feed(c, [(2000.0, 101.0, -50.0), (2000.01, 101.0, -50.0)])
        before = c.last_frame
        c.raw[dsp.X_AXIS] = 3000.0
        self.assertEqual(c.frame(2000.01), dsp.ZERO)
        self.assertEqual(c.frame(2000.0105), dsp.ZERO)
        self.assertEqual(c.last_frame, before)

    def test_backwards_time_and_long_gaps_reset(self):
        c = calibrated_controller()
        c.switch_mode("B", True)
        feed(c, [(2000.0, 101.0, -50.0), (2000.01, 101.0, -50.0)])
        c.frame(1999.0)
        self.assertIsNone(c.last_frame)
        feed(c, [(3000.0, 101.0, -50.0), (3000.01, 101.0, -50.0)])
        c.frame(3005.0)
        self.assertIsNone(c.last_frame)

    def test_gyro_lost_forces_recalibration(self):
        c = calibrated_controller()
        c.gyro_lost()
        self.assertTrue(c.calibrating)
        self.assertIsNone(c.center)

    def test_disconnect_clears_held_buttons_and_accel(self):
        c = calibrated_controller()
        c.switch_mode("B", True)
        c.switch_mode("A", True)
        c.feed_accel(1, 2, 3, 1.0)
        c.disconnect()
        self.assertFalse(c.active)
        self.assertIsNone(c.last_accel)

    def test_controller_events(self):
        c = dsp.GyroMouse()
        seen = []
        c.on_event = seen.append
        feed(c, [(1000.0 + i * 0.01, 100.0 + i % 3, -50.0 + i % 2) for i in range(100)])
        c.begin_calibration()
        c.gyro_lost()
        c.gyro_lost(silent=True)
        self.assertEqual(seen, ["calibrated", "calibration_started", "calibration_started"])

    def test_rates_for_live_display(self):
        c = dsp.GyroMouse()
        self.assertIsNone(c.rates_dps())
        c = calibrated_controller()
        c.raw[dsp.X_AXIS] = c.center[dsp.X_AXIS] + 30 * settings.GYRO_UNITS_PER_DEGREE_S
        c.raw[dsp.Y_AXIS] = c.center[dsp.Y_AXIS]
        yaw, pitch = c.rates_dps()
        self.assertAlmostEqual(yaw, 30, delta=0.5)
        self.assertAlmostEqual(pitch, 0, delta=0.5)


class ScrollRepeaterTests(unittest.TestCase):
    def test_tap_gives_one_notch_and_hold_repeats(self):
        s = dsp.ScrollRepeater()
        self.assertEqual(s.changed("scroll.up", True, 10.0), [(E.REL_WHEEL, +1)])
        self.assertEqual(s.changed("scroll.up", True, 10.01), [])
        self.assertEqual(s.repeats(10.1), [])
        self.assertEqual(s.repeats(10.0 + settings.DPAD_FIRST_REPEAT_DELAY + 0.01), [(E.REL_WHEEL, +1)])
        s.changed("scroll.up", False, 11.0)
        self.assertIsNone(s.next_deadline())

    def test_directions_and_unknown_tokens(self):
        s = dsp.ScrollRepeater()
        self.assertEqual(s.changed("scroll.left", True, 1.0), [(E.REL_HWHEEL, -1)])
        self.assertEqual(s.changed("mouse.left", True, 1.0), [])
        s.release_all()
        self.assertIsNone(s.next_deadline())


class RollTests(SettingsIsolation):
    G = settings.ACCEL_UNITS_PER_G

    def tilted(self, degrees):
        a = math.radians(degrees)
        return self.G * math.sin(a), 0.0, self.G * math.cos(a)

    def test_level_remote_has_zero_roll(self):
        r = dsp.RollCompensator()
        r.update(*self.tilted(0), now=1.0)
        self.assertTrue(r.valid)
        self.assertAlmostEqual(r.angle, 0.0, places=6)

    def test_roll_angle_from_gravity(self):
        r = dsp.RollCompensator()
        for i in range(30):
            r.update(*self.tilted(30), now=1.0 + i * 0.01)
        self.assertAlmostEqual(math.degrees(r.angle), 30.0, delta=0.5)

    def test_swinging_and_vertical_samples_are_ignored(self):
        r = dsp.RollCompensator()
        r.update(*self.tilted(20), now=1.0)
        held = r.angle
        r.update(3 * self.G, 0.0, 0.0, now=1.01)
        r.update(0.0, self.G, 0.1 * self.G, now=1.02)
        self.assertEqual(r.angle, held)

    def test_rotation_restores_a_horizontal_sweep(self):
        r = dsp.RollCompensator()
        for i in range(30):
            r.update(*self.tilted(45), now=1.0 + i * 0.01)
        omega, theta = 20.0, r.angle
        horizontal, vertical = r.rotate(omega * math.cos(theta), omega * math.sin(theta))
        self.assertAlmostEqual(horizontal, omega, places=6)
        self.assertAlmostEqual(vertical, 0.0, places=6)

    def test_roll_invert_flips_direction(self):
        r = dsp.RollCompensator()
        r.update(*self.tilted(45), now=1.0)
        normal = r.rotate(10.0, 0.0)
        settings.ROLL_INVERT = True
        self.assertAlmostEqual(normal[1], -r.rotate(10.0, 0.0)[1], places=9)

    def test_compensation_straightens_pointer_motion(self):
        theta = math.radians(45)
        rate = 20.0
        raw_x = 101.0 + rate * math.cos(theta) * settings.GYRO_UNITS_PER_DEGREE_S
        raw_y = -50.0 + rate * math.sin(theta) * settings.GYRO_UNITS_PER_DEGREE_S

        def swipe(enabled):
            settings.ROLL_COMPENSATION = enabled
            c = calibrated_controller()
            c.roll.angle, c.roll.valid = theta, True
            c.switch_mode("B", True)
            return feed(c, [(2000.0 + i * 0.01, raw_x, raw_y) for i in range(101)])

        plain, fixed = swipe(False), swipe(True)
        self.assertGreater(abs(plain[1]), 300)
        self.assertLess(abs(fixed[1]), 20)
        self.assertGreater(abs(fixed[0]), 550)


if __name__ == "__main__":
    unittest.main()
