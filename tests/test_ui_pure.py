"""UI logic that needs no display: the model and the SVG drawings."""

import unittest
import xml.dom.minidom

from wiimote_mouse.ui import remote_svg as svg
from wiimote_mouse.ui.model import RemoteModel


def state(connected=True, mode="ready", extensions=(), leds=None, asleep=False, profile="default"):
    remote = None
    if connected:
        remote = {"id": "AA", "mode": mode, "asleep": asleep, "sensors": {"gyro": True, "accel": False},
                  "extensions": list(extensions), "battery": 80, "leds": leds}
    return {"connected": connected, "remote": remote, "pointer": {"sensitivity": 30.0},
            "profile": {"active": profile, "available": ["default", "media"]}}


class ModelTests(unittest.TestCase):
    def test_starts_without_a_daemon(self):
        m = RemoteModel()
        self.assertEqual((m.daemon_up, m.connected, m.mode, m.extensions), (False, False, None, []))
        self.assertEqual(m.title(), "The daemon is not running")

    def test_titles_follow_the_state(self):
        m = RemoteModel()
        m.daemon_found(state(connected=False))
        self.assertEqual(m.title(), "Waiting for a Wii Remote")
        for mode, text in (("ready", "Ready"), ("calibrating", "Calibrating: hold the remote still"),
                           ("waiting", "Waiting for the MotionPlus"), ("asleep", "Sensors asleep: press any button"),
                           ("odd", "Connected")):
            m.apply_event("state", state(mode=mode))
            self.assertEqual(m.title(), text)

    def test_button_events_track_what_is_held(self):
        m = RemoteModel()
        m.daemon_found(state())
        m.apply_event("buttons", {"button": "a", "pressed": True})
        m.apply_event("buttons", {"button": "up", "pressed": True})
        m.apply_event("buttons", {"button": "a", "pressed": False})
        m.apply_event("buttons", {"button": "zzz", "pressed": False})
        self.assertEqual(m.pressed, {"up"})
        self.assertEqual(m.remote_view()["pressed"], {"up"})

    def test_disconnecting_clears_everything_live(self):
        m = RemoteModel()
        m.daemon_found(state(extensions=["nunchuk"]))
        m.apply_event("buttons", {"button": "a", "pressed": True})
        m.apply_event("motion", {"yaw": 1})
        m.apply_event("ext", {"device": "nunchuk", "values": {"BTN_C": 1}})
        m.apply_event("state", state(connected=False))
        self.assertEqual((m.pressed, m.motion, m.ext), (set(), None, {}))
        self.assertTrue(m.daemon_up)

    def test_losing_the_daemon_resets_the_model(self):
        m = RemoteModel()
        m.daemon_found(state())
        m.apply_event("buttons", {"button": "b", "pressed": True})
        m.daemon_lost()
        self.assertEqual((m.daemon_up, m.pressed, m.connected), (False, set(), False))

    def test_extension_values_merge_and_unplugged_ones_are_dropped(self):
        m = RemoteModel()
        m.daemon_found(state(extensions=["nunchuk", "classic"]))
        m.apply_event("ext", {"device": "nunchuk", "values": {"ABS_HAT0X": 5}})
        m.apply_event("ext", {"device": "nunchuk", "values": {"BTN_C": 1}})
        m.apply_event("ext", {"device": "classic", "values": {"BTN_A": 1}})
        self.assertEqual(m.ext["nunchuk"], {"ABS_HAT0X": 5, "BTN_C": 1})
        m.apply_event("state", state(extensions=["nunchuk"]))
        self.assertEqual(list(m.ext), ["nunchuk"])
        self.assertEqual(m.extensions, ["nunchuk"])


def parse(text):
    return xml.dom.minidom.parseString(text)


def element(doc, element_id):
    for node in doc.getElementsByTagName("*"):
        if node.getAttribute("id") == element_id:
            return node
    raise AssertionError(f"no element {element_id}")


class RemoteDrawingTests(unittest.TestCase):
    def test_every_drawing_is_well_formed_in_both_palettes(self):
        for dark in (False, True):
            parse(svg.render_remote({"connected": True, "pressed": {"a"}, "leds": 5}, dark))
            parse(svg.render_gauges({"roll": 10, "yaw": 1, "pitch": 2, "clutch": True, "scroll": True}, True, dark))
            parse(svg.render_gauges(None, False, dark))
            parse(svg.render_nunchuk({"ABS_HAT0X": 10, "BTN_Z": 1}, dark))

    def test_pressed_buttons_are_marked_and_coloured(self):
        pressed = {"a", "b", "up", "home", "two"}
        doc = parse(svg.render_remote({"connected": True, "pressed": pressed}))
        for name in ("a", "b", "up", "down", "left", "right", "minus", "home", "plus", "one", "two"):
            node = element(doc, f"btn-{name}")
            self.assertEqual(node.getAttribute("class"), "on" if name in pressed else "off", name)
        self.assertEqual(element(doc, "btn-a").getAttribute("fill"), svg.PALETTES["light"]["accent"])
        self.assertNotEqual(element(doc, "btn-one").getAttribute("fill"), svg.PALETTES["light"]["accent"])

    def test_leds_follow_the_mask(self):
        doc = parse(svg.render_remote({"connected": True, "leds": 0b0101}))
        self.assertEqual([element(doc, f"led-{i}").getAttribute("class") for i in (1, 2, 3, 4)],
                         ["on", "off", "on", "off"])
        doc = parse(svg.render_remote({"connected": True, "leds": None}))
        self.assertEqual({element(doc, f"led-{i}").getAttribute("class") for i in (1, 2, 3, 4)}, {"off"})

    def test_disconnected_and_sleeping_remotes_are_dimmed(self):
        self.assertEqual(element(parse(svg.render_remote({"connected": False})), "remote").getAttribute("opacity"), "0.4")
        self.assertEqual(element(parse(svg.render_remote({"connected": True, "asleep": True})), "remote")
                         .getAttribute("opacity"), "0.7")
        self.assertEqual(element(parse(svg.render_remote({"connected": True})), "remote").getAttribute("opacity"), "1.0")

    def test_dark_palette_differs(self):
        light = svg.render_remote({"connected": True}, dark=False)
        dark = svg.render_remote({"connected": True}, dark=True)
        self.assertNotEqual(light, dark)
        self.assertIn(svg.PALETTES["dark"]["body"], dark)

    def test_gauge_shows_roll_rates_and_clutch(self):
        doc = parse(svg.render_gauges({"roll": 25.0, "yaw": 40.0, "pitch": -90.0, "clutch": True, "scroll": False}))
        self.assertIn("rotate(25.0", element(doc, "horizon").getAttribute("transform"))
        self.assertIn("+25", element(doc, "roll-text").firstChild.data)
        self.assertEqual(element(doc, "pill-move").getAttribute("class"), "on")
        self.assertEqual(element(doc, "pill-scroll").getAttribute("class"), "off")
        yaw, pitch = element(doc, "bar-yaw"), element(doc, "bar-pitch")
        self.assertGreater(float(yaw.getAttribute("x")), 100)                  # grows to the right of the centre
        self.assertLess(float(pitch.getAttribute("x")), 104)                   # grows to the left
        self.assertGreater(float(pitch.getAttribute("width")), float(yaw.getAttribute("width")))

    def test_gauge_clamps_extreme_values_and_handles_missing_data(self):
        doc = parse(svg.render_gauges({"roll": 400.0, "yaw": 99999.0, "pitch": None, "clutch": False, "scroll": False}))
        self.assertLessEqual(float(element(doc, "bar-yaw").getAttribute("width")), 58.0)
        self.assertIn("rotate(180.0", element(doc, "horizon").getAttribute("transform"))
        self.assertEqual(len([n for n in doc.getElementsByTagName("rect") if n.getAttribute("id") == "bar-pitch"]), 0)
        empty = parse(svg.render_gauges(None))
        self.assertIn("no tilt data", element(empty, "roll-text").firstChild.data)
        self.assertIn("calibrating", svg.render_gauges(None, calibrating=True))

    def test_nunchuk_stick_and_buttons(self):
        centre = parse(svg.render_nunchuk({}))
        right_up = parse(svg.render_nunchuk({"ABS_HAT0X": 120, "ABS_HAT0Y": 120, "BTN_C": 1}))
        self.assertEqual(float(element(centre, "stick").getAttribute("cx")), 85.0)
        self.assertGreater(float(element(right_up, "stick").getAttribute("cx")), 85.0)
        self.assertLess(float(element(right_up, "stick").getAttribute("cy")), 70.0)        # up is up
        self.assertEqual(element(right_up, "btn-c").getAttribute("class"), "on")
        self.assertEqual(element(right_up, "btn-z").getAttribute("class"), "off")
        far = parse(svg.render_nunchuk({"ABS_HAT0X": 9999}))
        self.assertLessEqual(float(element(far, "stick").getAttribute("cx")), 85 + 22.1)


if __name__ == "__main__":
    unittest.main()
