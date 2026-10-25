"""Control commands and live events, exercised over the real Unix socket."""

import json
import math
import time
import unittest
from unittest import mock

from evdev import ecodes as E

from wiimote_mouse import dsp, feedback, settings
from wiimote_mouse.client import ClientError
from tests.helpers import DaemonCase, FakeLeds, FakeNotifier, FakeRumble, ev, key, wait_for


def event_matching(client, topic, predicate=lambda data: True, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        event = client.next_event(timeout=max(0.01, end - time.monotonic()))
        if event is None:
            return None
        if event[0] == topic and predicate(event[1]):
            return event[1]
    return None


def fake_feedback(test):
    """Make every session use fake LEDs / rumble / notifier. Returns the three fakes."""
    leds, rumble, notifier = FakeLeds(), FakeRumble(), FakeNotifier()

    def create(remote, timers, sensitivity):
        fb = feedback.Feedback(timers, sensitivity, leds, rumble, notifier)
        fb._remote = remote             # as Feedback.create() does, so reconfigure() works
        return fb

    patcher = mock.patch.object(feedback.Feedback, "create", staticmethod(create))
    patcher.start()
    test.addCleanup(patcher.stop)
    return leds, rumble, notifier


class BasicCommandTests(DaemonCase):
    serve = True

    def test_hello_and_unknown_commands_without_a_remote(self):
        self.start()
        client = self.client()
        hello = client.call("hello")
        self.assertEqual((hello["name"], hello["protocol"]), ("wiimote-mouse", 1))
        with self.assertRaisesRegex(ClientError, "unknown command 'dance'"):
            client.call("dance")
        status = client.call("status")
        self.assertEqual((status["connected"], status["remote"]), (False, None))
        self.assertEqual(status["profile"]["active"], "default")
        self.assertGreaterEqual(status["daemon"]["uptime"], 0)
        for command in ("calibrate", "rumble", "led", "sensors"):
            with self.assertRaisesRegex(ClientError, "no Wii Remote is connected"):
                client.call(command, mask=1, action="sleep")

    def test_status_describes_the_connected_remote(self):
        buttons, gyro, _ = self.connected("/g1", nunchuk="/n1")
        client = self.client()
        remote = client.call("status")["remote"]
        self.assertEqual(remote["id"], "AA")
        self.assertEqual(remote["mode"], "calibrating")
        self.assertEqual(remote["sensors"], {"gyro": True, "accel": False})
        self.assertEqual(remote["extensions"], ["nunchuk"])
        self.calibrate(gyro)
        self.assertTrue(wait_for(lambda: client.call("status")["remote"]["mode"] == "ready"))
        client.call("calibrate")
        self.assertTrue(self.controller.calibrating)
        self.assertEqual(client.call("status")["remote"]["mode"], "calibrating")
        self.stop()

    def test_devices(self):
        self.connected("/g1")
        remotes = self.client().call("devices")["remotes"]
        self.assertEqual(remotes, {"AA": {"buttons": "/b1", "gyro": "/g1"}})
        self.stop()


class ConfigCommandTests(DaemonCase):
    serve = True

    def test_get_and_list(self):
        self.start()
        client = self.client()
        option = client.call("config.get", key="pointer.sensitivity")
        self.assertEqual((option["value"], option["source"], option["live"]), (30.0, "default", True))
        self.assertEqual(len(client.call("config.get")["options"]), len(settings.OPTIONS))
        with self.assertRaisesRegex(ClientError, "unknown setting 'nope'"):
            client.call("config.get", key="nope")

    def test_set_applies_live_and_is_saved_shortly_after(self):
        self.start()
        client = self.client()
        result = client.call("config.set", key="pointer.sensitivity", value=45)
        self.assertEqual((result["value"], result["source"], result["restart_required"]), (45.0, "override", False))
        self.assertEqual(settings.SENSITIVITY, 45.0)
        self.assertTrue(wait_for(lambda: self.overrides_path.exists()))
        saved = json.loads(self.overrides_path.read_text())
        self.assertEqual(saved["settings"]["pointer.sensitivity"]["value"], 45.0)

    def test_validation_errors_leave_everything_unchanged(self):
        self.start()
        client = self.client()
        for key_, value, message in (("pointer.sensitivity", 9999, "between"), ("pointer.sensitivity", "x", "a number"),
                                     ("pointer.invert_x", 1, "true or false"), ("nope", 1, "unknown setting")):
            with self.assertRaisesRegex(ClientError, message):
                client.call("config.set", key=key_, value=value)
        with self.assertRaisesRegex(ClientError, "missing 'value'"):
            client.call("config.set", key="pointer.sensitivity")
        with self.assertRaisesRegex(ClientError, "'key' has the wrong type"):
            client.call("config.set", key=5, value=1)
        self.assertEqual(settings.SENSITIVITY, 30.0)

    def test_restart_required_is_reported(self):
        self.start()
        self.assertTrue(self.client().call("config.set", key="scroll.hires", value=False)["restart_required"])

    def test_reset(self):
        self.start()
        client = self.client()
        client.call("config.set", key="pointer.invert_y", value=True)
        result = client.call("config.reset", key="pointer.invert_y")
        self.assertEqual((result["value"], result["source"]), (False, "default"))
        self.assertIs(settings.INVERT_Y, False)

    def test_reload_picks_up_edits_and_reports_problems(self):
        self.start()
        client = self.client()
        self.config_path.write_text("[pointer]\nsensitivity = 22\nbogus = 1\n")
        result = client.call("config.reload")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(settings.SENSITIVITY, 22.0)
        self.config_path.write_text("[pointer\n")
        with self.assertRaisesRegex(ClientError, "invalid TOML"):
            client.call("config.reload")
        self.assertEqual(settings.SENSITIVITY, 22.0)               # the broken file changed nothing

    def test_reload_keeps_unsaved_changes(self):
        self.start()
        client = self.client()
        client.call("config.set", key="pointer.sensitivity", value=50)
        client.call("config.reload")                                # flushes before re-reading
        self.assertEqual(settings.SENSITIVITY, 50.0)

    def test_reload_applies_profile_edits(self):
        self.start()
        client = self.client()
        self.config_path.write_text('[profiles.fresh]\ndescription = "new"\n')
        client.call("config.reload")
        self.assertIn("fresh", [p["name"] for p in client.call("profile.list")["profiles"]])

    def test_changing_sensitivity_updates_the_led_bar(self):
        leds, rumble, notifier = fake_feedback(self)
        buttons, gyro, _ = self.connected("/g1")
        self.calibrate(gyro)
        self.assertTrue(wait_for(lambda: leds.mask == 0b0111))
        self.client().call("config.set", key="pointer.sensitivity", value=60)
        self.assertTrue(wait_for(lambda: leds.mask == 0b1111))
        self.stop()

    def test_turning_feedback_off_takes_effect_in_a_running_session(self):
        leds, rumble, notifier = fake_feedback(self)
        buttons, gyro, _ = self.connected("/g1")
        self.calibrate(gyro)
        client = self.client()
        client.call("config.set", key="feedback.rumble", value=False)
        self.assertTrue(wait_for(lambda: rumble.closed))
        client.call("config.set", key="feedback.leds", value=False)
        self.assertTrue(wait_for(lambda: leds.mask == 0b0001), "LED 1 is the kernel's default")
        self.stop()


class ProfileCommandTests(DaemonCase):
    serve = True

    def test_list_get_set_cycle(self):
        self.start()
        client = self.client()
        listing = client.call("profile.list")
        self.assertEqual([p["name"] for p in listing["profiles"]], ["default", "presentation", "media"])
        self.assertEqual(listing["active"], "default")
        presentation = client.call("profile.get", name="presentation")
        self.assertEqual(presentation["buttons"]["right"], "key:KEY_RIGHT")
        self.assertIsNone(presentation["ui"])
        self.assertEqual(client.call("profile.set", name="media")["name"], "media")
        self.assertEqual(client.call("profile.get")["name"], "media")
        self.assertEqual(client.call("profile.cycle")["name"], "default")
        self.assertEqual(client.call("profile.cycle", delta=-1)["name"], "media")
        with self.assertRaisesRegex(ClientError, "unknown profile 'nope'"):
            client.call("profile.set", name="nope")
        with self.assertRaisesRegex(ClientError, "unknown profile 'nope'"):
            client.call("profile.get", name="nope")

    def test_the_active_profile_is_remembered(self):
        self.start()
        self.client().call("profile.set", name="presentation")
        self.assertTrue(wait_for(lambda: self.state_path.exists()))
        self.assertEqual(json.loads(self.state_path.read_text())["profile"], "presentation")
        self.assertEqual(self.keyboards_made, 1)

    def test_save_use_and_delete_a_profile(self):
        self.start()
        client = self.client()
        saved = client.call("profile.save", name="ptt", profile={
            "description": "Push to talk", "buttons": {"two": "key:KEY_F13", "home": "none"}})
        self.assertEqual((saved["source"], saved["buttons"]["two"]), ("ui", "key:KEY_F13"))
        self.assertEqual(saved["buttons"]["one"], "mouse.left")                     # inherited
        self.assertTrue(wait_for(lambda: self.overrides_path.exists()))
        self.assertIn("ptt", json.loads(self.overrides_path.read_text())["profiles"])
        client.call("profile.set", name="ptt")
        self.assertEqual(client.call("profile.get")["ui"]["buttons"]["two"], "key:KEY_F13")
        result = client.call("profile.delete", name="ptt")
        self.assertEqual(result, {"deleted": "ptt", "active": "default"})
        with self.assertRaisesRegex(ClientError, "unknown profile"):
            client.call("profile.get", name="ptt")

    def test_editing_the_active_profile_takes_effect_immediately(self):
        buttons, _, _ = self.connected("/g1")
        client = self.client()
        client.call("profile.save", name="default", profile={"buttons": {"one": "key:KEY_A"}})
        buttons.push(key(E.BTN_1, 1))
        self.assertTrue(wait_for(lambda: self.keyboard.held == {E.KEY_A}))
        self.stop()

    def test_validation(self):
        self.start()
        client = self.client()
        for name, profile, message in (
                ("Bad Name", {}, "lowercase"), ("x", {"buttons": {"nope": "none"}}, "unknown button"),
                ("x", {"buttons": {"a": "fly"}}, "unknown action"), ("x", {"stuff": 1}, "unknown field"),
                ("x", "text", "must be an object")):
            with self.assertRaisesRegex(ClientError, message):
                client.call("profile.save", name=name, profile=profile)
        with self.assertRaisesRegex(ClientError, "was not saved from"):
            client.call("profile.delete", name="media")

    def test_config_toml_profiles_cannot_be_deleted_but_can_be_overlaid(self):
        self.configure('[profiles.mine]\ndescription = "from the file"\n')
        self.start()
        client = self.client()
        with self.assertRaisesRegex(ClientError, "config.toml"):
            client.call("profile.delete", name="mine")
        client.call("profile.save", name="mine", profile={"buttons": {"a": "calibrate"}})
        mine = client.call("profile.get", name="mine")
        self.assertEqual((mine["description"], mine["buttons"]["a"]), ("from the file", "calibrate"))


class TestingCommandTests(DaemonCase):
    serve = True

    def test_rumble_and_led_tests(self):
        leds, rumble, notifier = fake_feedback(self)
        buttons, gyro, _ = self.connected("/g1")
        client = self.client()
        client.call("rumble", count=3)
        self.assertTrue(wait_for(lambda: rumble.pulses == 1))
        self.assertEqual(client.call("led", mask=0b1010), {"leds": 0b1010})
        self.assertEqual(leds.mask, 0b1010)
        client.call("led", mask=None)
        self.assertNotEqual(leds.mask, 0b1010)
        for bad in (0, 11, -1, True, "x"):
            with self.assertRaisesRegex(ClientError, "count|between|must be"):
                client.call("rumble", count=bad)
        for bad in (16, -1, "x", True):
            with self.assertRaisesRegex(ClientError, "0-15"):
                client.call("led", mask=bad)
        with self.assertRaisesRegex(ClientError, "missing 'mask'"):
            client.call("led")
        self.stop()

    def test_rumble_and_leds_report_when_unavailable(self):
        buttons, gyro, _ = self.connected("/g1")         # find_leds -> [] so no LEDs in this setup
        client = self.client()
        with self.assertRaisesRegex(ClientError, "LEDs are not available"):
            client.call("led", mask=1)
        self.stop()

    def test_sensors_sleep_and_wake(self):
        leds, rumble, notifier = fake_feedback(self)
        buttons, gyro, _ = self.connected("/g1")
        client = self.client()
        self.assertEqual(client.call("sensors", action="sleep"), {"asleep": True})
        self.assertTrue(gyro.closed)
        self.assertEqual(client.call("status")["remote"]["mode"], "asleep")
        self.assertEqual(client.call("sensors", action="wake"), {"asleep": False})
        self.assertTrue(wait_for(lambda: gyro.open_count == 2))
        with self.assertRaisesRegex(ClientError, "'sleep' or 'wake'"):
            client.call("sensors", action="nap")
        self.stop()


class EventTests(DaemonCase):
    serve = True

    def test_state_events_follow_connection_profile_and_sensitivity(self):
        self.start()
        client = self.client()
        initial = client.subscribe(["state"])["state"]
        self.assertFalse(initial["connected"])
        buttons, gyro, _ = self.world.add_remote("AA", "/b1", "/g1")
        data = event_matching(client, "state", lambda d: d["connected"])
        self.assertEqual(data["remote"]["id"], "AA")
        client.call("profile.set", name="media")
        self.assertIsNotNone(event_matching(client, "state", lambda d: d["profile"]["active"] == "media"))
        client.call("config.set", key="pointer.sensitivity", value=41)
        self.assertIsNotNone(event_matching(client, "state", lambda d: d["pointer"]["sensitivity"] == 41))
        self.calibrate(gyro)
        self.assertIsNotNone(event_matching(client, "state", lambda d: d["remote"]["mode"] == "ready"))
        buttons.kill()
        gyro.kill()
        self.world.remove_remote("AA")
        self.assertIsNotNone(event_matching(client, "state", lambda d: not d["connected"]))

    def test_unchanged_state_is_not_repeated(self):
        buttons, gyro, _ = self.connected("/g1")
        client = self.client()
        client.subscribe(["state"])
        client.call("profile.set", name="default")                  # already active
        client.call("config.set", key="pointer.invert_x", value=True)   # already true
        self.assertIsNone(event_matching(client, "state", timeout=0.3))
        self.stop()

    def test_button_events(self):
        buttons, _, _ = self.connected("/g1")
        client = self.client()
        client.subscribe(["buttons"])
        buttons.push(key(E.BTN_SOUTH, 1), key(E.BTN_SOUTH, 0), key(E.KEY_UP, 1), key(0x2FF, 1))
        got = [event_matching(client, "buttons") for _ in range(3)]
        self.assertEqual(got, [{"button": "a", "pressed": True}, {"button": "a", "pressed": False},
                               {"button": "up", "pressed": True}])
        self.stop()

    def test_battery_updates_the_state(self):
        from wiimote_mouse import devices
        with mock.patch.object(devices, "find_battery", lambda key: "/fake/capacity"), \
                mock.patch.object(devices, "read_battery", lambda path: 87.4):
            settings.BATTERY_FIRST_POLL_DELAY = 0.05
            self.start()
            client = self.client()
            client.subscribe(["state"])
            self.world.add_remote("AA", "/b1", "/g1")
            self.assertIsNotNone(event_matching(client, "state", lambda d: d["remote"] and d["remote"]["battery"] == 87))
            self.stop()

    def test_motion_stream_opens_the_accelerometer_only_while_watched(self):
        buttons, gyro, accel = self.connected("/g1", "/a1")
        self.calibrate(gyro)
        client = self.client()
        self.assertEqual(accel.open_count, 0)
        client.subscribe(["motion"])
        self.assertTrue(wait_for(lambda: accel.open_count == 1))
        angle = math.radians(30)
        t0 = 8000.0
        for i in range(60):
            ts = t0 + i * 0.01
            accel.push(ev(E.EV_ABS, E.ABS_RX, 100 * math.sin(angle), ts), ev(E.EV_ABS, E.ABS_RY, 0, ts),
                       ev(E.EV_ABS, E.ABS_RZ, 100 * math.cos(angle), ts), ev(E.EV_SYN, E.SYN_REPORT, 0, ts))
            gyro.push(ev(E.EV_ABS, dsp.X_AXIS, self.controller.center[dsp.X_AXIS] + 20 * settings.GYRO_UNITS_PER_DEGREE_S, ts),
                      ev(E.EV_ABS, dsp.Y_AXIS, self.controller.center[dsp.Y_AXIS], ts),
                      ev(E.EV_SYN, E.SYN_REPORT, 0, ts))
            time.sleep(0.004)
        data = event_matching(client, "motion", lambda d: d["roll"] is not None and d["yaw"] is not None)
        self.assertIsNotNone(data)
        self.assertAlmostEqual(data["yaw"], 20, delta=1)
        self.assertAlmostEqual(data["pitch"], 0, delta=1)
        self.assertAlmostEqual(data["roll"], 30, delta=2)
        self.assertEqual(len(data["accel"]), 3)
        self.assertFalse(data["calibrating"])
        client.call("unsubscribe")
        self.assertTrue(wait_for(lambda: accel.closed))
        self.stop()

    def test_motion_events_are_rate_limited_and_accumulate_movement(self):
        buttons, gyro, _ = self.connected("/g1")
        self.calibrate(gyro)
        client = self.client()
        client.subscribe(["motion"])
        buttons.push(key(E.BTN_EAST, 1))
        time.sleep(0.05)
        t0 = 9000.0
        for i in range(150):
            ts = t0 + i * 0.01
            gyro.push(ev(E.EV_ABS, dsp.X_AXIS, self.controller.center[dsp.X_AXIS] + 20 * settings.GYRO_UNITS_PER_DEGREE_S, ts),
                      ev(E.EV_ABS, dsp.Y_AXIS, self.controller.center[dsp.Y_AXIS], ts), ev(E.EV_SYN, E.SYN_REPORT, 0, ts))
            time.sleep(0.002)
        events = []
        while True:
            event = client.next_event(timeout=0.3)
            if event is None:
                break
            events.append(event[1])
        self.assertTrue(events)
        self.assertLess(len(events), 30, "150 reports in ~0.3 s must be thinned to about 30 events/s")
        emitted_dx = sum(e[1] for e in self.mouse.log if e[0] == "emit")
        self.assertLessEqual(abs(sum(e["dx"] for e in events)), abs(emitted_dx))
        self.assertGreater(abs(sum(e["dx"] for e in events)), abs(emitted_dx) * 0.7)
        self.assertTrue(all(e["clutch"] for e in events))
        self.stop()

    def test_a_live_view_keeps_the_sensors_awake_and_wakes_sleeping_ones(self):
        settings.IDLE_SLEEP_SECONDS = 0.3
        buttons, gyro, _ = self.connected("/g1")
        client = self.client()
        client.subscribe(["motion"])
        time.sleep(0.6)
        self.assertFalse(gyro.closed, "idle sleep must not interrupt a live view")
        client.call("unsubscribe")
        self.assertTrue(wait_for(lambda: gyro.closed, timeout=3))
        client.subscribe(["motion"])
        self.assertTrue(wait_for(lambda: gyro.open_count == 2))
        self.stop()


class ExtensionEventTests(DaemonCase):
    serve = True

    def test_extension_streams_are_opened_only_on_request_and_coalesced(self):
        buttons, gyro, _ = self.connected("/g1", nunchuk="/n1")
        nunchuk = self.world.devices["/n1"]
        client = self.client()
        time.sleep(0.15)
        self.assertEqual(nunchuk.open_count, 0)
        client.subscribe(["ext"])
        self.assertTrue(wait_for(lambda: nunchuk.open_count == 1))
        nunchuk.push(*[ev(E.EV_ABS, E.ABS_HAT0X, v) for v in range(1, 11)], ev(E.EV_KEY, E.BTN_C, 1),
                     ev(E.EV_ABS, E.ABS_HAT0Y, -3), ev(E.EV_SYN, E.SYN_REPORT, 0))
        data = event_matching(client, "ext")
        self.assertEqual(data, {"device": "nunchuk", "values": {"ABS_HAT0X": 10, "BTN_C": 1, "ABS_HAT0Y": -3}})
        client.call("unsubscribe", topics=["ext"])
        self.assertTrue(wait_for(lambda: nunchuk.closed))
        self.stop()

    def test_plugging_and_unplugging_updates_the_state(self):
        buttons, gyro, _ = self.connected("/g1")
        client = self.client()
        client.subscribe(["state", "ext"])
        classic = self.world.add_extension("AA", "classic", "/c1")
        data = event_matching(client, "state", lambda d: d["remote"] and d["remote"]["extensions"] == ["classic"])
        self.assertIsNotNone(data)
        self.assertTrue(wait_for(lambda: classic.open_count == 1))
        self.world.remove_extension("AA", "classic")
        self.assertIsNotNone(event_matching(client, "state", lambda d: d["remote"] and d["remote"]["extensions"] == []))
        self.assertTrue(wait_for(lambda: classic.closed))
        self.stop()

    def test_a_vanishing_extension_device_is_dropped(self):
        buttons, gyro, _ = self.connected("/g1", nunchuk="/n1")
        nunchuk = self.world.devices["/n1"]
        client = self.client()
        client.subscribe(["ext"])
        self.assertTrue(wait_for(lambda: nunchuk.open_count == 1))
        nunchuk.kill()
        self.world.remove_extension("AA", "nunchuk")            # as when it is unplugged
        self.assertTrue(wait_for(lambda: nunchuk.closed))
        self.assertTrue(self.thread.is_alive())
        self.stop()

    def test_no_scanning_without_watchers_once_everything_is_open(self):
        settings.IDLE_SLEEP_SECONDS = 0
        buttons, gyro, _ = self.connected("/g1", nunchuk="/n1")
        time.sleep(0.15)
        self.select_timeouts.clear()
        time.sleep(0.4)
        self.assertEqual(self.select_timeouts, [])
        self.stop()


if __name__ == "__main__":
    unittest.main()
