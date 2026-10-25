#!/usr/bin/env python3
"""Release helper used by the release workflow.

    release_notes.py v1.0.0b1            print that version's section of CHANGELOG.md
    release_notes.py --check v1.0.0b1    fail unless the tag, the package version and the changelog agree
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]", re.M)


def changelog_section(version, text=None):
    """The body of the ``## [version]`` section, or None if there is no such section."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8") if text is None else text
    headings = list(HEADING.finditer(text))
    for index, match in enumerate(headings):
        if match.group("version") == version:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[match.end():end].split("\n", 1)[1].strip() + "\n"
    return None


def check(tag):
    """Return a list of problems (empty when the tag is releasable)."""
    from wiimote_mouse import __version__
    problems = []
    if tag != f"v{__version__}":
        problems.append(f"tag {tag} does not match the package version v{__version__}")
    version = tag[1:] if tag.startswith("v") else tag
    section = changelog_section(version)
    if section is None:
        problems.append(f"CHANGELOG.md has no '## [{version}]' section")
    elif not section.strip():
        problems.append(f"the CHANGELOG.md section for {version} is empty")
    return problems


def main(argv):
    args = argv[1:]
    verify = args[:1] == ["--check"]
    if verify:
        args = args[1:]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    tag = args[0]
    if verify:
        problems = check(tag)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1 if problems else 0
    section = changelog_section(tag[1:] if tag.startswith("v") else tag)
    if section is None:
        print(f"error: no changelog section for {tag}", file=sys.stderr)
        return 1
    print(section, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
