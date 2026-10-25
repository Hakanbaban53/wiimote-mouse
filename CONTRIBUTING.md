# Contributing

Thanks for helping! Bug reports and hardware test reports are as useful as code: please use the issue
templates, and include the output of `wiimote-mouse --doctor`.

## Setting up

```bash
git clone https://github.com/Hakanbaban53/wiimote-mouse.git
cd wiimote-mouse
python3 -m pip install evdev pyflakes        # or your distribution's python-evdev
python3 -m unittest discover -s tests -t .   # no Wii Remote needed: the tests use fake devices
```

The UI tests need GTK 4, libadwaita and PyGObject plus a display. Without them they skip themselves; to run
them headless use `xvfb-run -a python3 -m unittest discover -s tests -t .`.

## Guidelines

- Everything a user can read (log messages, CLI output, UI text, docs, config comments) is in English.
- New behaviour comes with tests. The daemon is driven through fake `/dev/input` devices (see
  `tests/helpers.py`), so most things can be tested end to end without hardware.
- Keep the daemon quiet when idle: no timers or polling that wake it when nothing is happening, and open
  expensive streams (accelerometer, extensions) only while something is watching them.
- Anything that can leave a key, click or command stuck when the remote disconnects needs a test for it.
- Run `python -m pyflakes wiimote_mouse tests tools` before sending a pull request.
- Tell us what you could and could not verify on real hardware.

## Releases

Maintainers: update `__version__` in `wiimote_mouse/__init__.py` and add a section to `CHANGELOG.md`, commit,
then tag it (`git tag -a v1.0.0 -m "..."` and `git push origin v1.0.0`). The release workflow checks that the
tag, the version and the changelog agree, runs the tests, and publishes the archives; tags containing `a`,
`b`, `rc` or `dev` become pre-releases.
