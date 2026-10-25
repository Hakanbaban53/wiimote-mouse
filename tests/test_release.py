"""Repository hygiene: versions, changelog, workflows and templates agree with each other."""

import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

try:
    import yaml
except ImportError:
    yaml = None

from wiimote_mouse import __version__

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/Hakanbaban53/wiimote-mouse"


def load_release_notes():
    spec = importlib.util.spec_from_file_location("release_notes", ROOT / "tools" / "release_notes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VersionTests(unittest.TestCase):
    def test_the_version_is_pep440_and_semver_like(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+((a|b|rc)\d+)?$")

    @unittest.skipIf(tomllib is None, "needs tomllib/tomli")
    def test_pyproject_is_consistent_with_the_package(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["tool"]["setuptools"]["dynamic"]["version"]["attr"], "wiimote_mouse.__version__")
        self.assertEqual(project["project"]["requires-python"], ">=3.9")
        self.assertEqual(project["project"]["name"], "wiimote-mouse")
        scripts = project["project"]["scripts"]
        self.assertEqual(set(scripts), {"wiimote-mouse", "wiimotectl", "wiimote-mouse-ui"})
        for target in scripts.values():                 # wiimote_mouse.ui.launch imports without GTK
            module, function = target.split(":")
            self.assertTrue(callable(getattr(__import__(module, fromlist=[function]), function)), target)
        for url in project["project"]["urls"].values():
            self.assertTrue(url.startswith(REPO), url)

    def test_every_launcher_in_the_installer_has_a_matching_entry_point(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("write_launcher wiimote-mouse daemon", installer)
        self.assertIn("write_launcher wiimotectl ctl", installer)
        self.assertIn("write_launcher wiimote-mouse-ui ui.launch", installer)


class ChangelogTests(unittest.TestCase):
    def test_the_current_version_has_a_changelog_section(self):
        notes = load_release_notes()
        section = notes.changelog_section(__version__)
        self.assertIsNotNone(section, f"CHANGELOG.md needs a '## [{__version__}]' section")
        self.assertTrue(section.strip())
        self.assertEqual(notes.check(f"v{__version__}"), [])

    def test_there_is_an_unreleased_section(self):
        self.assertIn("## [Unreleased]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_section_extraction(self):
        notes = load_release_notes()
        text = "# Changelog\n\n## [Unreleased]\n\n## [1.1.0] - 2027-01-01\nnew things\n\n## [1.0.0] - 2026-01-01\nold things\n"
        self.assertEqual(notes.changelog_section("1.1.0", text), "new things\n")
        self.assertEqual(notes.changelog_section("1.0.0", text), "old things\n")
        self.assertIsNone(notes.changelog_section("2.0.0", text))

    def test_release_check_rejects_a_mismatched_tag_and_the_cli_exit_codes(self):
        notes = load_release_notes()
        self.assertTrue(any("does not match" in p for p in notes.check("v99.0.0")))
        run = lambda *args: subprocess.run([sys.executable, str(ROOT / "tools/release_notes.py"), *args],
                                           capture_output=True, text=True)
        self.assertEqual(run("--check", f"v{__version__}").returncode, 0)
        self.assertEqual(run("--check", "v99.0.0").returncode, 1)
        self.assertEqual(run().returncode, 2)
        printed = run(f"v{__version__}")
        self.assertEqual(printed.returncode, 0)
        self.assertIn("First public release", printed.stdout)
        self.assertEqual(run("v99.0.0").returncode, 1)

    def test_the_changelog_test_count_is_not_wildly_stale(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        claimed = int(re.search(r"covered by (\d+) automated tests", text).group(1))
        self.assertGreaterEqual(claimed, 250)


@unittest.skipIf(yaml is None, "needs PyYAML")
class GitHubFilesTests(unittest.TestCase):
    def load(self, relative):
        return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))

    def test_ci_covers_the_supported_pythons_the_ui_and_the_shell_scripts(self):
        ci = self.load(".github/workflows/ci.yml")
        self.assertEqual(ci["jobs"]["unit"]["strategy"]["matrix"]["python"], ["3.9", "3.10", "3.11", "3.12", "3.13"])
        self.assertIn("ui", ci["jobs"])
        self.assertIn("shell", ci["jobs"])
        steps = " ".join(str(step) for job in ci["jobs"].values() for step in job["steps"])
        for expected in ("unittest discover", "pyflakes", "shellcheck", "xvfb-run"):
            self.assertIn(expected, steps)

    def test_release_workflow_checks_before_it_publishes(self):
        release = self.load(".github/workflows/release.yml")
        self.assertEqual(release["permissions"], {"contents": "write"})
        names = [step.get("name", "") for step in release["jobs"]["release"]["steps"]]
        self.assertLess(names.index("Check that the tag, the package version and the changelog agree"),
                        names.index("Publish the release"))
        text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("--prerelease", text)
        for tag, is_pre in (("v1.0.0b1", True), ("v1.0.0rc2", True), ("v1.0.0", False), ("v1.2.3", False),
                            ("v2.0.0dev1", True)):
            self.assertEqual(bool(re.search(r"[0-9](a|b|rc|dev)[0-9]*", tag)), is_pre, tag)

    def test_issue_templates_are_valid_forms(self):
        for name in ("bug_report", "hardware_report"):
            form = self.load(f".github/ISSUE_TEMPLATE/{name}.yml")
            self.assertTrue(form["name"] and form["description"] and form["body"])
            ids = [item["id"] for item in form["body"] if "id" in item]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertIn("version", ids)
        bug = self.load(".github/ISSUE_TEMPLATE/bug_report.yml")
        required = {item["id"] for item in bug["body"] if item.get("validations", {}).get("required")}
        self.assertIn("doctor", required)
        config = self.load(".github/ISSUE_TEMPLATE/config.yml")
        self.assertIs(config["blank_issues_enabled"], False)


class RepositoryFilesTests(unittest.TestCase):
    def test_license_is_mit_with_a_holder(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 Hakanbaban53", text)

    def test_readme_points_at_the_repository_and_its_documents(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for expected in (f"git clone {REPO}.git", f"{REPO}/releases", f"{REPO}/issues/new/choose",
                         "wiimote-mouse-uninstall", "not affiliated with or endorsed by Nintendo",
                         "(LICENSE)", "(CONTRIBUTING.md)", "(CHANGELOG.md)"):
            self.assertIn(expected, readme)

    def test_gitignore_keeps_build_output_out(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for expected in ("__pycache__/", "dist/", "*.egg-info/"):
            self.assertIn(expected, text)

    def test_every_script_is_executable_in_the_sense_the_installer_needs(self):
        for name in ("install.sh", "uninstall.sh"):
            first = (ROOT / name).read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(first, "#!/usr/bin/env bash")


if __name__ == "__main__":
    unittest.main()
