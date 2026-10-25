import errno
import os
import tempfile
import unittest
from pathlib import Path

from evdev import ecodes as E

from wiimote_mouse import devices
from tests.helpers import FakeSysfs, World
from unittest import mock


class ReadEventsTests(unittest.TestCase):
    def test_read_is_a_generator_so_errors_must_be_caught_while_iterating(self):
        class Dev:
            def __init__(self, exc):
                self.exc = exc

            def read(self):
                raise self.exc
                yield  # pragma: no cover - makes this a generator like evdev's

        self.assertEqual(devices.read_events(Dev(BlockingIOError(errno.EAGAIN, "again"))), [])
        with self.assertRaises(devices.DeviceLost):
            devices.read_events(Dev(OSError(errno.ENODEV, "gone")))


class ScanTests(unittest.TestCase):
    def build(self, root):
        fs = FakeSysfs(root)
        a, b = "0005:057E:0306.0001", "0005:057E:0306.0002"
        nodes = [
            fs.input(a, 1, 1, devices.BUTTONS_NAME), fs.input(a, 2, 2, devices.GYRO_NAME),
            fs.input(a, 3, 3, devices.ACCEL_NAME), fs.input(a, 7, 7, devices.BUTTONS_NAME + " IR"),
            fs.input(a, 8, 8, devices.BUTTONS_NAME + " Nunchuk"),
            fs.input(b, 4, 4, devices.BUTTONS_NAME), fs.input(b, 5, 5, devices.GYRO_NAME),
            fs.input("usb-keyboard", 6, 6, "Some Keyboard"),
        ]
        return fs, nodes, a, b

    def test_two_remotes_are_paired_correctly_and_noise_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            fs, nodes, _, _ = self.build(root)
            with fs.patch(nodes):
                found = devices.scan_remotes()
        self.assertEqual(len(found), 2)
        first, second = (found[k] for k in sorted(found))
        self.assertEqual(first, {"buttons": "/dev/input/event1", "gyro": "/dev/input/event2",
                                 "accel": "/dev/input/event3", "nunchuk": "/dev/input/event8"})
        self.assertEqual(second, {"buttons": "/dev/input/event4", "gyro": "/dev/input/event5"})

    def test_extension_names_and_slots(self):
        self.assertEqual(devices.extension_slots({"buttons": "x", "classic": "y", "nunchuk": "z"}),
                         ("nunchuk", "classic"))
        self.assertEqual(devices.extension_slots({"buttons": "x"}), ())
        self.assertEqual(devices.EXTENSIONS["Nintendo Wii Remote Nunchuk"], "nunchuk")

    def test_leds_and_battery_belong_to_the_right_remote(self):
        with tempfile.TemporaryDirectory() as root:
            fs, nodes, a, b = self.build(root)
            leds_a = [fs.led(a, i) for i in range(4)]
            for i in range(3):
                fs.led(b, i)                                    # remote B has an incomplete LED set
            cap_b = fs.battery(b, "00:11:22:33:44:55")
            with fs.patch(nodes):
                key_a, key_b = sorted(devices.scan_remotes())
                self.assertEqual([os.path.realpath(p) for p in devices.find_leds(key_a)], leds_a)
                self.assertEqual(devices.find_leds(key_b), [])
                self.assertIsNone(devices.find_battery(key_a))
                self.assertEqual(os.path.realpath(devices.find_battery(key_b)), cap_b)

    def test_battery_normalization(self):
        self.assertEqual(devices.BATTERY_FULL_CAPACITY, 78)         # what the kernel reports at 0xC8
        self.assertEqual(devices.battery_percent(78), 100.0)
        self.assertEqual(devices.battery_percent(999), 100.0)
        self.assertEqual(devices.battery_percent(-3), 0.0)
        self.assertEqual(devices.battery_percent(39), 50.0)
        with tempfile.TemporaryDirectory() as root:
            cap = Path(root) / "capacity"
            cap.write_text("78\n")
            self.assertEqual(devices.read_battery(str(cap)), 100.0)

    def test_code_names(self):
        self.assertEqual(devices.code_name(E.EV_KEY, E.BTN_C), "BTN_C")
        self.assertEqual(devices.code_name(E.EV_ABS, E.ABS_HAT0X), "ABS_HAT0X")
        self.assertEqual(devices.code_name(E.EV_KEY, 0x7FFF), f"{E.EV_KEY}:{0x7FFF}")


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        patcher = mock.patch.multiple(devices, scan_remotes=self.world.scan, open_device=self.world.open)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_accelerometer_is_only_opened_on_request(self):
        self.world.add_remote("K", "/b", "/g", "/a")
        remote = devices.connect()
        self.assertIsNotNone(remote.gyro)
        self.assertIsNone(remote.accel)                             # connect() never opens it
        self.assertFalse(devices.attach_sensors(remote, want_accel=True))   # gyro was already open
        self.assertIsNotNone(remote.accel)
        remote.drop_accel()
        self.assertIsNone(remote.accel)

    def test_late_motionplus_is_reported_as_newly_attached(self):
        self.world.add_remote("K", "/b")
        remote = devices.connect()
        self.assertIsNone(remote.gyro)
        self.world.add_gyro("K", "/g")
        self.assertTrue(devices.attach_sensors(remote, want_accel=False))
        self.assertFalse(devices.attach_sensors(remote, want_accel=False))

    def test_no_remote_and_close(self):
        self.assertIsNone(devices.connect())
        self.world.add_remote("K", "/b", "/g")
        remote = devices.connect()
        remote.close()
        self.assertTrue(self.world.devices["/b"].closed and self.world.devices["/g"].closed)

    def test_permission_problems_are_retryable_errors(self):
        self.world.add_remote("K", "/b")
        with mock.patch.object(devices, "open_device", side_effect=PermissionError(errno.EACCES, "no")):
            with self.assertRaises(devices.RemoteUnavailable) as caught:
                devices.connect()
        self.assertIn("permission", str(caught.exception))
        with mock.patch.object(devices, "open_device", side_effect=FileNotFoundError("gone")):
            with self.assertRaises(devices.RemoteUnavailable):
                devices.connect()


if __name__ == "__main__":
    unittest.main()
