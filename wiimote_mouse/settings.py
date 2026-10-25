"""Tunable settings: defaults, config file and runtime overrides.

Layers, lowest to highest priority:

1. built-in defaults (the constants below)
2. ``~/.config/wiimote-mouse/config.toml`` (edited by hand)
3. ``~/.config/wiimote-mouse/overrides.json`` (written by ``wiimotectl`` and the UI)

Every other module reads a value as ``settings.NAME`` at the moment it needs it, so
a change applies immediately for options marked ``live``.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:     # Python < 3.11: use the backport if it is installed
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

log = logging.getLogger("wiimote-mouse")

# --------------------- Tunables ---------------------
INVERT_X = True                 # Corrected X direction
INVERT_Y = False                # Corrected Y direction
INVERT_SCROLL = False           # True if gyro scroll feels reversed
SENSITIVITY = 30.0              # Baseline pointer speed
POINTER_ACCELERATION = 0.40     # 0 disables acceleration; >0 helps quick sweeps
ACCEL_REFERENCE_DPS = 125.0     # Speed (deg/s) where acceleration reaches its max

# One Euro filtering: smooth slow movement; respond faster to quick movements.
ONE_EURO_MIN_CUTOFF = 7.0       # Hz; lower -> smoother, slightly more latency
ONE_EURO_BETA = 0.010           # Higher -> faster response to rapid changes
ONE_EURO_D_CUTOFF = 1.5         # Hz; smoothing for velocity derivative

# Calibration and bias correction
CALIBRATION_WINDOW = 0.45       # seconds
CALIBRATION_MIN_SAMPLES = 22
CALIBRATION_MAX_SPREAD = 650.0
STILLNESS_CHECK_INTERVAL = 0.055
BIAS_UPDATE_TIME_CONSTANT = 0.65
AUTO_BIAS_MAX_CHANGE = 3500.0
NOISE_MULTIPLIER = 6.0 * 1.4826  # MAD -> noise deadzone, unbounded

# Gyro safety / frame handling
GYRO_UNITS_PER_DEGREE_S = 167.56    # = 8192 * 9 / 440, as decoded by hid-wiimote
MAX_GYRO_DPS = 2000.0               # MotionPlus hardware limit; beyond = glitch
MAX_ABS_DELTA = MAX_GYRO_DPS * GYRO_UNITS_PER_DEGREE_S
# (v3 used 30000, i.e. ~179 deg/s: a fast flick above that reset the filters and
#  froze the cursor. Fast flicks now saturate at MAX_PIXELS_PER_FRAME instead.)
MIN_FRAME_TIME = 0.002          # Reports closer than this are skipped (numerics)
MAX_FRAME_TIME = 0.035          # Longer gaps are integrated as if this long
MAX_FRAME_GAP = 0.15            # Longer gaps (or backwards time) reset the filters
MAX_PIXELS_PER_FRAME = 35

# Scroll controls
DPAD_FIRST_REPEAT_DELAY = 0.34  # Hold to repeat after this delay
DPAD_REPEAT_INTERVAL = 0.085    # Repeats every ~85 ms
DPAD_FAST_AFTER = 1.4
DPAD_FAST_INTERVAL = 0.055
GYRO_SCROLL_NOTCHES_PER_DEGREE = 0.20  # A+tilt; adjust for faster scroll
HIRES_SCROLL = True             # Emit hi-res + legacy wheel events together

# Device discovery
SCAN_INTERVAL = 1.0             # s between scans while waiting for a device
RETRY_DELAY = 2.0               # s before retrying after an open/grab problem
RECONNECT_DELAY = 0.25          # s to let a vanished remote's nodes disappear before rescanning
KEYBOARD_SETTLE_SECONDS = 0.25  # s after creating the virtual keyboard until the desktop has noticed it
UINPUT_WAIT_SECONDS = 30.0      # login race: wait this long for /dev/uinput access

# Power: close MotionPlus/accelerometer after this long without button activity
IDLE_SLEEP_SECONDS = 300.0      # 0 = never

# Roll compensation (remote twisted around its long axis). Needs the accelerometer.
ROLL_COMPENSATION = False
ROLL_INVERT = False             # flip if a horizontal sweep gets *more* diagonal
ACCEL_UNITS_PER_G = 100.0       # typical Wii Remote value (10-bit, zero at 0x200)
ACCEL_FILTER_TC = 0.25          # s, smoothing of the gravity direction
ACCEL_GATE = 0.30               # ignore samples whose |a| differs >30 % from 1 g

# Feedback on the remote itself
FEEDBACK_LEDS = True
FEEDBACK_RUMBLE = True
FEEDBACK_NOTIFY = True          # desktop notifications via notify-send
BATTERY_LOW_PERCENT = 20.0      # on the normalized 0-100 scale, see battery_percent()
BATTERY_CRITICAL_PERCENT = 5.0
BATTERY_POLL_INTERVAL = 300.0   # each read makes the remote send a status report
BATTERY_FIRST_POLL_DELAY = 8.0
CALIBRATION_NOTICE_AFTER = 6.0  # s of unfinished calibration before we nudge the user
SENSITIVITY_MIN, SENSITIVITY_MAX = 5.0, 80.0
STATE_SAVE_DELAY = 2.0

# ----------------------------------------------------


class ConfigError(Exception):
    """The config file can't be used at all (unreadable, not valid TOML)."""


@dataclass(frozen=True)
class Option:
    """A user-facing setting. ``key`` is ``section.name`` as used in config.toml."""

    key: str
    attr: str
    kind: str                       # "float" or "bool"
    limits: tuple = None            # inclusive (low, high) for floats
    live: bool = True               # False: only applied after a restart
    description: str = ""


OPTIONS = (
    Option("pointer.sensitivity", "SENSITIVITY", "float", (SENSITIVITY_MIN, SENSITIVITY_MAX),
           True, "Cursor speed. The + and - buttons change it too."),
    Option("pointer.acceleration", "POINTER_ACCELERATION", "float", (0.0, 3.0),
           True, "Extra speed on fast sweeps. 0 disables acceleration."),
    Option("pointer.accel_reference_dps", "ACCEL_REFERENCE_DPS", "float", (10.0, 2000.0),
           True, "Rotation speed (deg/s) at which the acceleration peaks."),
    Option("pointer.invert_x", "INVERT_X", "bool", None, True, "Flip the horizontal direction."),
    Option("pointer.invert_y", "INVERT_Y", "bool", None, True, "Flip the vertical direction."),
    Option("scroll.invert", "INVERT_SCROLL", "bool", None, True, "Flip the gyro scroll direction."),
    Option("scroll.gyro_notches_per_degree", "GYRO_SCROLL_NOTCHES_PER_DEGREE", "float", (0.01, 5.0),
           True, "Gyro scroll speed (wheel notches per degree of tilt)."),
    Option("scroll.hires", "HIRES_SCROLL", "bool", None, False,
           "Send high-resolution wheel events together with classic ones."),
    Option("filter.min_cutoff", "ONE_EURO_MIN_CUTOFF", "float", (0.5, 60.0),
           True, "Smoothing at low speed in Hz. Lower is smoother but laggier."),
    Option("filter.beta", "ONE_EURO_BETA", "float", (0.0, 1.0),
           True, "How quickly smoothing relaxes on fast movement."),
    Option("power.idle_sleep_seconds", "IDLE_SLEEP_SECONDS", "float", (0.0, 86400.0),
           True, "Close the MotionPlus after this many idle seconds to save battery. 0 = never."),
    Option("tilt.roll_compensation", "ROLL_COMPENSATION", "bool", None, True,
           "Correct for the remote being twisted around its long axis (needs the accelerometer)."),
    Option("tilt.roll_invert", "ROLL_INVERT", "bool", None, True,
           "Flip the roll correction direction if sweeps get more diagonal instead of straighter."),
    Option("feedback.leds", "FEEDBACK_LEDS", "bool", None, True, "Show state on the player LEDs."),
    Option("feedback.rumble", "FEEDBACK_RUMBLE", "bool", None, True, "Buzz the remote for notable events."),
    Option("feedback.notifications", "FEEDBACK_NOTIFY", "bool", None, True,
           "Desktop notifications (battery, calibration)."),
    Option("feedback.battery_low_percent", "BATTERY_LOW_PERCENT", "float", (1.0, 90.0),
           True, "Warn when the battery drops to this percentage."),
)
BY_KEY = {option.key: option for option in OPTIONS}
DEFAULTS = {option.attr: globals()[option.attr] for option in OPTIONS}

# Non-option top-level sections that belong to other modules.
PASSTHROUGH_SECTIONS = ("profiles",)

# ----- layer state
_config_values = {}      # key -> value read from config.toml
_overrides = {}          # key -> {"value": v, "base": value of the layers below}
_ui_profiles = {}        # name -> profile definition saved from wiimotectl / the UI
_raw_config = {}         # parsed config.toml, for sections handled elsewhere
_overrides_path = None
_dirty = False


# ----- paths
def default_config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "wiimote-mouse"


def default_config_path():
    return default_config_dir() / "config.toml"


def default_overrides_path():
    return default_config_dir() / "overrides.json"


def default_state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "wiimote-mouse" / "state.json"


# ----- validation
def coerce(option, value):
    """Return the value in its proper type, or raise ValueError with a readable reason."""
    if option.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number")
    value = float(value)
    if option.limits is not None and not option.limits[0] <= value <= option.limits[1]:
        raise ValueError(f"must be between {option.limits[0]:g} and {option.limits[1]:g}")
    return value


def _set(option, value):
    globals()[option.attr] = value


def _reset_to_defaults():
    for attr, value in DEFAULTS.items():
        globals()[attr] = value


def _base_value(option):
    """What the option would be without an override."""
    return _config_values.get(option.key, DEFAULTS[option.attr])


# ----- loading
def _read_toml(path):
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read ({exc})") from exc
    if tomllib is None:
        raise ConfigError(f"{path}: reading the config needs Python 3.11+ or the 'tomli' package")
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML ({exc})") from exc


def _read_overrides(path, warnings):
    if path is None:
        return {}, {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}
    except (OSError, ValueError) as exc:
        warnings.append(f"{path}: ignoring unreadable overrides ({exc})")
        return {}, {}
    if not isinstance(data, dict):
        warnings.append(f"{path}: ignoring overrides (not an object)")
        return {}, {}
    settings = data.get("settings")
    profiles = data.get("profiles")
    return (settings if isinstance(settings, dict) else {},
            profiles if isinstance(profiles, dict) else {})


def configure(config_path, overrides_path=None):
    """(Re)load every layer from scratch. Returns a list of warnings.

    Raises ConfigError when config.toml exists but can't be parsed. A single bad value
    only produces a warning and keeps the value from the layer below.
    """
    global _config_values, _overrides, _ui_profiles, _raw_config, _overrides_path, _dirty
    warnings = []
    raw = _read_toml(config_path)
    _reset_to_defaults()

    config_values = {}
    for section, table in raw.items():
        if section in PASSTHROUGH_SECTIONS:
            continue
        if not isinstance(table, dict):
            warnings.append(f"[{section}] should be a section; ignored.")
            continue
        for name, value in table.items():
            option = BY_KEY.get(f"{section}.{name}")
            if option is None:
                warnings.append(f"Unknown setting '{section}.{name}'; ignored.")
                continue
            try:
                config_values[option.key] = coerce(option, value)
            except ValueError as exc:
                warnings.append(f"'{option.key}' {exc}; using the default.")
    _config_values = config_values
    for key, value in config_values.items():
        _set(BY_KEY[key], value)

    saved, ui_profiles = _read_overrides(overrides_path, warnings)
    overrides = {}
    for key, entry in saved.items():
        option = BY_KEY.get(key)
        if option is None or not isinstance(entry, dict) or "value" not in entry:
            warnings.append(f"Ignoring unknown saved setting '{key}'.")
            continue
        if entry.get("base") != _base_value(option):
            log.info("config.toml changed '%s'; dropping the saved override.", key)
            continue
        try:
            value = coerce(option, entry["value"])
        except ValueError as exc:
            warnings.append(f"Saved setting '{key}' {exc}; ignored.")
            continue
        overrides[key] = {"value": value, "base": entry["base"]}
        _set(option, value)
    _overrides = overrides
    _ui_profiles = {name: data for name, data in ui_profiles.items() if isinstance(data, dict)}
    _raw_config = raw
    _overrides_path = Path(overrides_path) if overrides_path else None
    _dirty = False
    return warnings


# ----- introspection
def source(key):
    if key in _overrides:
        return "override"
    return "config" if key in _config_values else "default"


def value(key):
    return globals()[BY_KEY[key].attr]


def describe(key=None):
    """Option metadata plus current value and where it comes from."""
    def one(option):
        low, high = option.limits if option.limits else (None, None)
        return {"key": option.key, "value": value(option.key), "default": DEFAULTS[option.attr],
                "source": source(option.key), "kind": option.kind, "min": low, "max": high,
                "live": option.live, "description": option.description}

    if key is not None:
        if key not in BY_KEY:
            raise KeyError(key)
        return one(BY_KEY[key])
    return [one(option) for option in OPTIONS]


# ----- runtime changes (wiimotectl, UI, the + / - buttons)
def set_override(key, new_value):
    """Validate and apply a runtime change; it is saved by flush_overrides().

    Returns True when the change only takes effect after a restart.
    """
    global _dirty
    option = BY_KEY.get(key)
    if option is None:
        raise KeyError(key)
    new_value = coerce(option, new_value)          # raises ValueError
    base = _base_value(option)
    _set(option, new_value)
    if new_value == base:
        _overrides.pop(key, None)
    else:
        _overrides[key] = {"value": new_value, "base": base}
    _dirty = True
    return not option.live


def reset_override(key):
    global _dirty
    option = BY_KEY.get(key)
    if option is None:
        raise KeyError(key)
    _overrides.pop(key, None)
    _set(option, _base_value(option))
    _dirty = True


def raw_config_profiles():
    profiles = _raw_config.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def ui_profiles():
    return dict(_ui_profiles)


def set_ui_profile(name, definition):
    global _dirty
    _ui_profiles[name] = definition
    _dirty = True


def delete_ui_profile(name):
    global _dirty
    if _ui_profiles.pop(name, None) is not None:
        _dirty = True
        return True
    return False


def overrides_dirty():
    return _dirty


def flush_overrides():
    """Atomically write overrides.json if anything changed."""
    global _dirty
    if not _dirty or _overrides_path is None:
        return
    try:
        _overrides_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _overrides_path.with_name(_overrides_path.name + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "settings": _overrides, "profiles": _ui_profiles},
                                  indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, _overrides_path)
        _dirty = False
    except OSError as exc:
        log.warning("Could not save %s: %s", _overrides_path, exc)
