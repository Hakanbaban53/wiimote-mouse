# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/) (pre-releases are written the PEP 440 way,
for example `1.0.0b1`).

## [Unreleased]

## [1.0.0b1] - 2026-10-24

First public release. It is a beta: the logic is covered by 283 automated tests with fake devices and was
checked against the Linux `hid-wiimote` driver source, but a few things still need confirming on real
hardware (see "Verified, and not yet verified on real hardware" in the README).

### Added
- **Daemon** (`wiimote-mouse`, a systemd user service): turns a Wii Remote with MotionPlus into a gyro mouse
  through `/dev/uinput`, on X11 and Wayland, without root.
  - Gyro pointer with a One Euro filter, bias learning while the remote is still, soft dead zone,
    acceleration, and optional roll compensation from the accelerometer.
  - Scrolling with the D-pad (with repeat) and by tilting while holding A.
  - Survives the remote sleeping, disconnecting and reconnecting; the MotionPlus can appear late or vanish.
  - Closes the MotionPlus after idle time to save the remote's battery; any button wakes it.
  - Feedback on the remote itself: player LEDs (state and sensitivity bar), rumble, battery warnings and
    desktop notifications.
- **Button profiles** (`default`, `presentation`, `media`, and your own): mouse buttons, scrolling, keyboard
  keys and chords, commands, and profile switching, in `config.toml` or from the CLI and UI.
- **Control socket** and **`wiimotectl`**: status, live event stream, settings, profiles, calibration and
  test commands, all scriptable (`--json`).
- **Graphical UI** (`wiimote-mouse-ui`, GTK 4 + libadwaita): live remote drawing, tilt gauges, Nunchuk panel,
  settings, profile editor and diagnostics.
- Extension devices (Nunchuk, Classic Controller, ...) are detected and streamed on request.
- Layered settings (defaults, `config.toml`, saved overrides) with hot reload.
- `wiimote-mouse --doctor` and `--check-config`.
- `install.sh` and `uninstall.sh` (the installer keeps a copy as `wiimote-mouse-uninstall`).
