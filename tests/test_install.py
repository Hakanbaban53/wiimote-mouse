"""The installer and the packaging files."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def run_script(self, script, *args, env_extra=None, stdin=subprocess.DEVNULL):
        env = {"PATH": os.environ["PATH"], "HOME": self.home}
        env.update(env_extra or {})
        return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True,
                              timeout=120, stdin=stdin)

    def sh(self, *args, **kwargs):
        return self.run_script(ROOT / "install.sh", *args, **kwargs)

    def uninstall(self, *args, **kwargs):
        return self.run_script(ROOT / "uninstall.sh", *args, **kwargs)

    def path(self, *parts):
        return Path(self.home, *parts)

    def install(self):
        result = self.sh("--user-only", "--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)


class InstallerTests(ScriptCase):
    def test_user_install_creates_launchers_package_unit_and_config(self):
        self.install()
        for name in ("wiimote-mouse", "wiimotectl", "wiimote-mouse-ui", "wiimote-mouse-uninstall"):
            self.assertTrue(os.access(self.path(".local/bin", name), os.X_OK), name)
        desktop = self.path(".local/share/applications/dev.wiimotemouse.Ui.desktop").read_text()
        self.assertIn(f"Exec={self.path('.local/bin/wiimote-mouse-ui')}", desktop)
        self.assertNotIn("@BIN_DIR@", desktop)
        self.assertTrue(self.path(".local/share/wiimote-mouse/lib/wiimote_mouse/daemon.py").exists())
        self.assertFalse(list(self.path(".local/share/wiimote-mouse/lib").rglob("__pycache__")))
        unit = self.path(".config/systemd/user/wiimote-mouse.service").read_text()
        self.assertIn("RestartPreventExitStatus=2", unit)
        self.assertIn("ExecStart=%h/.local/bin/wiimote-mouse", unit)
        self.assertTrue(self.path(".config/wiimote-mouse/config.toml").exists())
        self.assertTrue(self.path(".local/share/doc/wiimote-mouse/README.md").exists())

    def test_installed_commands_run(self):
        self.install()
        for name, expected in (("wiimote-mouse", "wiimote-mouse "), ("wiimotectl", "wiimotectl ")):
            result = subprocess.run([str(self.path(".local/bin", name)), "--version"],
                                    capture_output=True, text=True, timeout=30, cwd="/")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith(expected), result.stdout)

    def test_installed_wiimotectl_reports_a_missing_daemon(self):
        self.install()
        env = {"PATH": os.environ["PATH"], "HOME": self.home, "XDG_RUNTIME_DIR": self.home}
        result = subprocess.run([str(self.path(".local/bin/wiimotectl")), "status"], env=env,
                                capture_output=True, text=True, timeout=30, cwd="/")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not running", result.stderr)

    def test_reinstall_is_idempotent_keeps_the_config_and_replaces_the_package(self):
        self.install()
        config = self.path(".config/wiimote-mouse/config.toml")
        config.write_text("[pointer]\nsensitivity = 11\n")
        stale = self.path(".local/share/wiimote-mouse/lib/wiimote_mouse/stale_module.py")
        stale.write_text("")
        self.install()
        self.assertEqual(config.read_text(), "[pointer]\nsensitivity = 11\n")
        self.assertFalse(stale.exists(), "files from an older version must not linger")
        self.assertEqual(stat.S_IMODE(self.path(".local/bin/wiimotectl").stat().st_mode), 0o755)

    def test_dry_run_touches_nothing_and_shows_the_system_steps(self):
        result = self.sh("--no-start", env_extra={"WIIMOTE_DRY_RUN": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.path(".local").exists())
        for expected in ("70-wiimote-mouse.rules", "modules-load.d", "groupadd", "udevadm", "write launcher",
                         "desktop entry", "wiimote-mouse-uninstall"):
            self.assertIn(expected, result.stdout)

    def test_unknown_option_is_rejected(self):
        self.assertEqual(self.sh("--bogus").returncode, 2)

    def test_help(self):
        result = self.sh("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--user-only", result.stdout)
        self.assertIn("uninstall.sh", result.stdout)


class UninstallerTests(ScriptCase):
    REMOVED = (".local/bin/wiimote-mouse", ".local/bin/wiimotectl", ".local/bin/wiimote-mouse-ui",
               ".local/bin/wiimote-mouse-uninstall", ".local/share/applications/dev.wiimotemouse.Ui.desktop",
               ".config/systemd/user/wiimote-mouse.service", ".local/share/wiimote-mouse/lib/wiimote_mouse",
               ".local/share/doc/wiimote-mouse")

    def test_removes_the_program_but_keeps_the_settings(self):
        self.install()
        result = self.uninstall("--yes", "--user-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        for gone in self.REMOVED:
            self.assertFalse(self.path(gone).exists(), gone)
        self.assertTrue(self.path(".config/wiimote-mouse/config.toml").exists())
        self.assertIn("kept", result.stdout)
        self.assertFalse(self.path(".local/share/wiimote-mouse").exists(), "empty directories are cleaned up")

    def test_purge_deletes_settings_and_state(self):
        self.install()
        self.path(".config/wiimote-mouse/overrides.json").write_text("{}")
        self.path(".local/state/wiimote-mouse").mkdir(parents=True)
        self.path(".local/state/wiimote-mouse/state.json").write_text("{}")
        self.assertEqual(self.uninstall("--yes", "--user-only", "--purge").returncode, 0)
        self.assertFalse(self.path(".config/wiimote-mouse").exists())
        self.assertFalse(self.path(".local/state/wiimote-mouse").exists())

    def test_it_asks_first_and_does_nothing_without_an_answer(self):
        self.install()
        result = self.uninstall("--user-only")                 # stdin is not a terminal
        self.assertEqual(result.returncode, 1)
        self.assertIn("pass --yes", result.stderr)
        self.assertTrue(self.path(".local/bin/wiimotectl").exists())

    def test_the_installed_copy_removes_everything_including_itself(self):
        self.install()
        result = subprocess.run([str(self.path(".local/bin/wiimote-mouse-uninstall")), "--yes", "--user-only"],
                                env={"PATH": os.environ["PATH"], "HOME": self.home},
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, cwd="/")
        self.assertEqual(result.returncode, 0, result.stderr)
        for gone in self.REMOVED:
            self.assertFalse(self.path(gone).exists(), gone)

    def test_install_sh_uninstall_still_works_as_a_shortcut(self):
        self.install()
        self.assertEqual(self.sh("uninstall", "--yes", "--user-only").returncode, 0)
        self.assertFalse(self.path(".local/bin/wiimotectl").exists())

    def test_nothing_installed_is_not_an_error(self):
        result = self.uninstall("--yes", "--user-only")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Nothing to remove", result.stdout)

    def test_dry_run_shows_the_steps_and_removes_nothing(self):
        self.install()
        result = self.uninstall("--dry-run", "--remove-group", "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("udevadm", "70-wiimote-mouse.rules", "wiimote-mouse-uninstall", "YOUR SETTINGS"):
            self.assertIn(expected, result.stdout)
        self.assertNotIn("groupdel", result.stdout, "no group to delete on this machine, so none is attempted")
        self.assertTrue(self.path(".local/bin/wiimotectl").exists())
        self.assertTrue(self.path(".config/wiimote-mouse/config.toml").exists())

    def test_unknown_option_and_help(self):
        self.assertEqual(self.uninstall("--bogus").returncode, 2)
        result = self.uninstall("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--purge", result.stdout)


class PackagingFilesTests(unittest.TestCase):
    def test_udev_rules_are_well_formed(self):
        lines = [l for l in (ROOT / "packaging/70-wiimote-mouse.rules").read_text().splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(line.count('"') % 2, 0, line)
        self.assertIn('TAG+="uaccess"', lines[0])
        self.assertIn('ATTRS{name}=="Nintendo Wii Remote*"', lines[1])
        self.assertIn("blue:p[0-3]", lines[2])

    def test_desktop_entry_is_valid(self):
        import configparser
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(ROOT / "packaging/dev.wiimotemouse.Ui.desktop.in", encoding="utf-8")
        entry = parser["Desktop Entry"]
        self.assertEqual((entry["Type"], entry["Terminal"]), ("Application", "false"))
        self.assertTrue(entry["Exec"].endswith("/wiimote-mouse-ui"))
        self.assertTrue(entry["Categories"].endswith(";"))
        self.assertEqual(entry["Name"], "Wii Remote Mouse")

    def test_ui_launcher_points_at_the_import_safe_entry_point(self):
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("write_launcher wiimote-mouse-ui ui.launch", text)
        self.assertNotIn("from wiimote_mouse.ui import app", (ROOT / "wiimote_mouse/ui/launch.py").read_text().split("def main")[0])

    def test_the_application_id_matches_the_desktop_file_name(self):
        text = (ROOT / "wiimote_mouse/ui/app.py").read_text(encoding="utf-8")
        self.assertIn('APP_ID = "dev.wiimotemouse.Ui"', text)
        self.assertTrue((ROOT / "packaging/dev.wiimotemouse.Ui.desktop.in").exists())

    def test_modules_file_loads_uinput(self):
        text = (ROOT / "packaging/wiimote-mouse-modules.conf").read_text()
        self.assertIn("uinput", [l.strip() for l in text.splitlines()])

    def test_everything_user_facing_is_english(self):
        turkish = set("çğıöşüÇĞİÖŞÜ")
        for path in [*(ROOT / "wiimote_mouse").rglob("*.py"), ROOT / "README.md", ROOT / "install.sh",
                     ROOT / "uninstall.sh", ROOT / "config.example.toml", ROOT / "CHANGELOG.md",
                     ROOT / "CONTRIBUTING.md", ROOT / "pyproject.toml", ROOT / "tools" / "release_notes.py",
                     *(ROOT / "packaging").glob("*"), *(ROOT / ".github").rglob("*.*")]:
            offending = [c for c in path.read_text(encoding="utf-8") if c in turkish]
            self.assertEqual(offending, [], f"{path.name} contains Turkish characters")

    def test_package_has_no_leftover_debug_code(self):
        for path in (ROOT / "wiimote_mouse").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("breakpoint()", text, path.name)
            self.assertNotIn("import pdb", text, path.name)


if __name__ == "__main__":
    unittest.main()
