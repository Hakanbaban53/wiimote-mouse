import unittest

from evdev import ecodes as E

from wiimote_mouse import profiles
from wiimote_mouse.profiles import ProfileError, parse_action, parse_binding


class ActionTests(unittest.TestCase):
    def test_simple_actions(self):
        for name in profiles.SIMPLE_ACTIONS:
            self.assertEqual(parse_action(name).name, name)
        self.assertEqual(parse_action("  Mouse.Left ").name, "mouse.left")

    def test_key_chords(self):
        action = parse_action("key:KEY_LEFTCTRL+key_c")
        self.assertEqual(action.kind, "keys")
        self.assertEqual(action.codes, (E.KEY_LEFTCTRL, E.KEY_C))
        self.assertEqual(action.text, "key:KEY_LEFTCTRL+KEY_C")

    def test_exec_is_split_like_a_shell_would_without_running_one(self):
        action = parse_action("exec:notify-send 'two words' x")
        self.assertEqual(action.argv, ("notify-send", "two words", "x"))

    def test_profile_switch(self):
        self.assertEqual(parse_action("profile:media").name, "media")

    def test_errors(self):
        for bad in ("", "   ", "fly", "key:", "key:KEY_NOPE", "key:BTN_LEFT", "key:KEY_A+", "exec:",
                    "exec:'unterminated", "profile:", "profile:Bad Name", 5, None):
            with self.assertRaises(ProfileError, msg=repr(bad)):
                parse_action(bad)

    def test_bindings(self):
        hold = parse_binding("mouse.left")
        self.assertEqual((hold.hold.name, hold.spec), ("mouse.left", "mouse.left"))
        table = parse_binding({"press": "key:KEY_F13", "release": "exec:echo off"})
        self.assertIsNone(table.hold)
        self.assertEqual(table.spec, {"press": "key:KEY_F13", "release": "exec:echo off"})
        self.assertTrue(table.uses_keys)
        self.assertFalse(parse_binding("calibrate").uses_keys)
        for bad in ({}, {"press": "fly"}, {"push": "calibrate"}, 3, ["a"]):
            with self.assertRaises(ProfileError, msg=repr(bad)):
                parse_binding(bad)


class BuildTests(unittest.TestCase):
    def test_builtins_inherit_the_default_profile(self):
        built, warnings = profiles.build({}, {})
        self.assertEqual(warnings, [])
        self.assertEqual(set(built), {"default", "presentation", "media"})
        presentation = built["presentation"]
        self.assertEqual(presentation.bindings["right"].hold.codes, (E.KEY_RIGHT,))
        self.assertEqual(presentation.bindings["b"].hold.name, "pointer.hold")       # inherited
        self.assertTrue(presentation.uses_keys)
        self.assertFalse(built["default"].uses_keys)
        for profile in built.values():
            self.assertEqual(set(profile.bindings), set(profiles.BUTTONS))

    def test_config_adds_and_modifies_profiles(self):
        built, warnings = profiles.build({
            "ptt": {"description": "Push to talk", "buttons": {"two": "key:KEY_F13"}},
            "presentation": {"buttons": {"home": "key:KEY_F5"}}}, {})
        self.assertEqual(warnings, [])
        self.assertEqual(built["ptt"].bindings["two"].hold.codes, (E.KEY_F13,))
        self.assertEqual(built["ptt"].bindings["one"].hold.name, "mouse.left")
        self.assertEqual((built["ptt"].source, built["ptt"].builtin), ("config", False))
        self.assertTrue(built["presentation"].modified)
        self.assertEqual(built["presentation"].bindings["home"].hold.codes, (E.KEY_F5,))
        self.assertEqual(built["presentation"].bindings["left"].hold.codes, (E.KEY_LEFT,))

    def test_changing_default_changes_every_profile_that_inherits(self):
        built, _ = profiles.build({"default": {"buttons": {"home": "none"}}}, {})
        self.assertEqual(built["media"].bindings["home"].hold.name, "none")

    def test_ui_layer_wins_over_config(self):
        built, _ = profiles.build({"x": {"buttons": {"a": "mouse.left"}}},
                                  {"x": {"buttons": {"a": "mouse.right"}}})
        self.assertEqual(built["x"].bindings["a"].hold.name, "mouse.right")
        self.assertEqual(built["x"].source, "ui")

    def test_invalid_definitions_warn_and_are_skipped(self):
        built, warnings = profiles.build({
            "Bad Name": {}, "notatable": 3,
            "ok": {"buttons": {"nope": "calibrate", "a": "fly", "b": {"push": "x"}, "one": "mouse.middle"}},
            "weird": {"buttons": "all"}}, {})
        self.assertEqual(len(warnings), 6)
        self.assertEqual(built["ok"].bindings["one"].hold.name, "mouse.middle")
        self.assertEqual(built["ok"].bindings["a"].hold.name, "scroll.hold")          # fell back
        self.assertNotIn("notatable", built)

    def test_public_shape(self):
        built, _ = profiles.build({"x": {"buttons": {"a": {"press": "calibrate"}}}}, {})
        data = built["x"].public()
        self.assertEqual(data["own"], ["a"])
        self.assertEqual(data["buttons"]["a"], {"press": "calibrate"})
        self.assertEqual(data["buttons"]["b"], "pointer.hold")


class ManagerTests(unittest.TestCase):
    def test_active_profile_and_cycling(self):
        m = profiles.ProfileManager()
        self.assertEqual(m.names(), ["default", "presentation", "media"])
        self.assertEqual(m.cycle(1), "presentation")
        self.assertEqual(m.cycle(-1), "default")
        self.assertEqual(m.cycle(-1), "media")
        m.activate("default")
        with self.assertRaises(KeyError):
            m.activate("nope")
        self.assertEqual(m.binding("b").hold.name, "pointer.hold")

    def test_custom_profiles_sort_after_builtins(self):
        m = profiles.ProfileManager()
        m.rebuild({"zeta": {}, "alpha": {}}, {})
        self.assertEqual(m.names(), ["default", "presentation", "media", "alpha", "zeta"])

    def test_rebuild_falls_back_when_the_active_profile_disappears(self):
        m = profiles.ProfileManager()
        m.rebuild({"temp": {}}, {})
        m.activate("temp")
        m.rebuild({}, {})
        self.assertEqual(m.active, "default")

    def test_clean_definition(self):
        cleaned = profiles.clean_definition(
            {"description": "d", "buttons": {"two": "KEY:key_f13", "a": {"release": "calibrate"}}})
        self.assertEqual(cleaned, {"description": "d", "buttons": {"two": "key:KEY_F13",
                                                                   "a": {"release": "calibrate"}}})
        for bad in ("x", {"extra": 1}, {"description": 3}, {"buttons": []}, {"buttons": {"nope": "none"}},
                    {"buttons": {"a": "fly"}}):
            with self.assertRaises(ProfileError, msg=repr(bad)):
                profiles.clean_definition(bad)


if __name__ == "__main__":
    unittest.main()
