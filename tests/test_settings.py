import json
import os
import unittest
from pathlib import Path

from wiimote_mouse import settings
from tests.helpers import SettingsIsolation, temp_dir

ROOT = Path(__file__).resolve().parent.parent


class SettingsCase(SettingsIsolation):
    def setUp(self):
        super().setUp()
        self.dir = Path(temp_dir(self))
        self.config = self.dir / "config.toml"
        self.overrides = self.dir / "overrides.json"

    def configure(self, text=None, **kwargs):
        if text is not None:
            self.config.write_text(text)
        return settings.configure(self.config, self.overrides, **kwargs)


class ConfigFileTests(SettingsCase):
    def test_missing_file_means_defaults(self):
        self.assertEqual(self.configure(), [])
        self.assertEqual(settings.SENSITIVITY, settings.DEFAULTS["SENSITIVITY"])
        self.assertEqual(settings.source("pointer.sensitivity"), "default")

    def test_valid_values_are_applied(self):
        self.assertEqual(self.configure("[pointer]\nsensitivity = 42\ninvert_x = false\n"
                                        "[power]\nidle_sleep_seconds = 0\n[tilt]\nroll_compensation = true\n"), [])
        self.assertEqual(settings.SENSITIVITY, 42.0)
        self.assertIs(settings.INVERT_X, False)
        self.assertEqual(settings.IDLE_SLEEP_SECONDS, 0.0)
        self.assertIs(settings.ROLL_COMPENSATION, True)
        self.assertEqual(settings.source("pointer.sensitivity"), "config")

    def test_bad_values_warn_and_keep_defaults(self):
        before = (settings.SENSITIVITY, settings.INVERT_Y, settings.BATTERY_LOW_PERCENT)
        warnings = self.configure('[pointer]\nsensitivity = 500\ninvert_y = "yes"\nbogus = 1\n'
                                  '[feedback]\nbattery_low_percent = true\n[nonsense]\nx = 1\n[oops]\n')
        self.assertEqual(len(warnings), 5)
        self.assertEqual((settings.SENSITIVITY, settings.INVERT_Y, settings.BATTERY_LOW_PERCENT), before)
        self.assertTrue(any("between" in w for w in warnings))

    def test_removing_a_value_restores_the_default_on_reload(self):
        self.configure("[pointer]\nsensitivity = 42\n")
        self.configure("[pointer]\n")
        self.assertEqual(settings.SENSITIVITY, settings.DEFAULTS["SENSITIVITY"])

    def test_broken_toml_is_an_error(self):
        with self.assertRaises(settings.ConfigError):
            self.configure("[pointer\nsensitivity =")

    def test_profiles_section_is_not_an_unknown_setting(self):
        self.assertEqual(self.configure('[profiles.demo]\ndescription = "x"\n'), [])
        self.assertIn("demo", settings.raw_config_profiles())

    def test_example_config_is_valid_and_matches_the_defaults(self):
        defaults = dict(settings.DEFAULTS)
        self.assertEqual(settings.configure(ROOT / "config.example.toml", None), [])
        for attr, value in defaults.items():
            self.assertEqual(getattr(settings, attr), value, f"{attr}: the example differs from the default")


class OverrideTests(SettingsCase):
    def test_set_applies_immediately_and_persists_on_flush(self):
        self.configure()
        self.assertFalse(settings.set_override("pointer.sensitivity", 40))
        self.assertEqual(settings.SENSITIVITY, 40.0)
        self.assertEqual(settings.source("pointer.sensitivity"), "override")
        self.assertTrue(settings.overrides_dirty())
        self.assertFalse(self.overrides.exists())
        settings.flush_overrides()
        saved = json.loads(self.overrides.read_text())
        self.assertEqual(saved["settings"]["pointer.sensitivity"], {"value": 40.0, "base": 30.0})
        self.assertEqual(sorted(os.listdir(self.dir)), ["overrides.json"])      # no temp file left

    def test_overrides_survive_a_restart(self):
        self.configure()
        settings.set_override("pointer.invert_y", True)
        settings.flush_overrides()
        self.configure()
        self.assertIs(settings.INVERT_Y, True)
        self.assertEqual(settings.source("pointer.invert_y"), "override")

    def test_editing_config_toml_wins_over_a_stale_override(self):
        self.configure("[pointer]\nsensitivity = 30\n")
        settings.set_override("pointer.sensitivity", 55)
        settings.flush_overrides()
        self.configure("[pointer]\nsensitivity = 20\n")            # the user edited the file
        self.assertEqual(settings.SENSITIVITY, 20.0)
        self.assertEqual(settings.source("pointer.sensitivity"), "config")

    def test_setting_the_base_value_removes_the_override(self):
        self.configure()
        settings.set_override("pointer.sensitivity", 45)
        settings.set_override("pointer.sensitivity", 30)
        self.assertEqual(settings.source("pointer.sensitivity"), "default")

    def test_reset_returns_to_the_layer_below(self):
        self.configure("[pointer]\nsensitivity = 25\n")
        settings.set_override("pointer.sensitivity", 60)
        settings.reset_override("pointer.sensitivity")
        self.assertEqual(settings.SENSITIVITY, 25.0)
        self.assertEqual(settings.source("pointer.sensitivity"), "config")

    def test_validation(self):
        self.configure()
        with self.assertRaises(ValueError):
            settings.set_override("pointer.sensitivity", 9999)
        with self.assertRaises(ValueError):
            settings.set_override("pointer.sensitivity", "fast")
        with self.assertRaises(ValueError):
            settings.set_override("pointer.invert_x", 1)
        with self.assertRaises(KeyError):
            settings.set_override("no.such", 1)
        self.assertEqual(settings.SENSITIVITY, 30.0)

    def test_restart_required_options_are_flagged(self):
        self.configure()
        self.assertTrue(settings.set_override("scroll.hires", False))
        self.assertFalse(settings.set_override("pointer.invert_x", False))

    def test_unreadable_overrides_only_warn(self):
        self.overrides.write_text("{ not json")
        self.assertTrue(any("unreadable" in w for w in self.configure()))
        self.overrides.write_text(json.dumps({"settings": {"bogus": {"value": 1, "base": 1},
                                                             "pointer.sensitivity": {"value": 9999, "base": 30.0}}}))
        warnings = self.configure()
        self.assertEqual(len(warnings), 2)
        self.assertEqual(settings.SENSITIVITY, 30.0)

    def test_ui_profiles_are_saved_with_the_overrides(self):
        self.configure()
        settings.set_ui_profile("ptt", {"description": "x", "buttons": {"two": "key:KEY_F13"}})
        settings.flush_overrides()
        self.configure()
        self.assertEqual(settings.ui_profiles()["ptt"]["buttons"], {"two": "key:KEY_F13"})
        self.assertTrue(settings.delete_ui_profile("ptt"))
        self.assertFalse(settings.delete_ui_profile("ptt"))

    def test_describe(self):
        self.configure()
        option = settings.describe("pointer.sensitivity")
        self.assertEqual((option["value"], option["default"], option["source"], option["kind"]),
                         (30.0, 30.0, "default", "float"))
        self.assertEqual((option["min"], option["max"]), (settings.SENSITIVITY_MIN, settings.SENSITIVITY_MAX))
        self.assertEqual(len(settings.describe()), len(settings.OPTIONS))
        with self.assertRaises(KeyError):
            settings.describe("nope")

    def test_no_overrides_path_means_nothing_is_written(self):
        settings.configure(self.config, None)
        settings.set_override("pointer.sensitivity", 50)
        settings.flush_overrides()
        self.assertFalse(self.overrides.exists())


if __name__ == "__main__":
    unittest.main()
