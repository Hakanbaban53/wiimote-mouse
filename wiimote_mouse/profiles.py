"""Button profiles: what each Wii Remote button does.

A profile maps button names to actions. Every profile inherits the ``default`` profile
for buttons it doesn't mention, so a profile only lists what differs. Set a button to
``"none"`` to switch it off.

Action syntax (the same text is used in config.toml, wiimotectl and the UI):

    pointer.hold, scroll.hold               gyro clutch / gyro scrolling while held
    mouse.left, mouse.right, mouse.middle   mouse buttons while held
    scroll.up/down/left/right               wheel notches, repeating while held
    sensitivity.up, sensitivity.down        pointer speed
    calibrate                               recalibrate the gyro
    profile.next, profile.previous          cycle profiles
    profile:<name>                          switch to a named profile
    key:KEY_LEFTCTRL+KEY_C                  keyboard keys while held (``+`` makes a chord)
    exec:notify-send hello                  run a command when pressed (no shell)

A button can also be a table with one-shot ``press`` and ``release`` actions, for example
to unmute a microphone while a button is held:

    [profiles.ptt.buttons]
    two = { press = "exec:wpctl set-mute @DEFAULT_AUDIO_SOURCE@ 0",
            release = "exec:wpctl set-mute @DEFAULT_AUDIO_SOURCE@ 1" }
"""

import re
import shlex
from dataclasses import dataclass, field

from evdev import ecodes as E

BUTTONS = ("a", "b", "one", "two", "plus", "minus", "home", "up", "down", "left", "right")

# hid-wiimote event codes for the buttons of a Wii Remote.
BUTTON_CODES = {
    E.BTN_SOUTH: "a",
    E.BTN_EAST: "b",
    E.BTN_1: "one",
    E.BTN_2: "two",
    E.KEY_NEXT: "plus",
    E.KEY_PREVIOUS: "minus",
    E.BTN_MODE: "home",
    E.KEY_UP: "up",
    E.KEY_DOWN: "down",
    E.KEY_LEFT: "left",
    E.KEY_RIGHT: "right",
}

SIMPLE_ACTIONS = {
    "none": "Do nothing.",
    "pointer.hold": "Move the cursor while held (the gyro clutch).",
    "scroll.hold": "Scroll by tilting the remote while held.",
    "mouse.left": "Left mouse button while held.",
    "mouse.right": "Right mouse button while held.",
    "mouse.middle": "Middle mouse button while held.",
    "scroll.up": "Scroll up; repeats while held.",
    "scroll.down": "Scroll down; repeats while held.",
    "scroll.left": "Scroll left; repeats while held.",
    "scroll.right": "Scroll right; repeats while held.",
    "sensitivity.up": "Increase pointer speed.",
    "sensitivity.down": "Decrease pointer speed.",
    "calibrate": "Recalibrate the gyro (hold the remote still).",
    "profile.next": "Switch to the next profile.",
    "profile.previous": "Switch to the previous profile.",
}

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ProfileError(ValueError):
    """A profile, button or action definition is invalid."""


@dataclass(frozen=True)
class Action:
    kind: str                       # "simple", "keys", "exec" or "profile"
    name: str = ""                  # simple: the action; profile: the target profile
    codes: tuple = ()               # keys: evdev key codes, in chord order
    argv: tuple = ()                # exec: the command line, already split
    text: str = ""                  # canonical text form


@dataclass(frozen=True)
class Binding:
    """What one button does. ``hold`` follows the button (pressed/released); the table
    form uses one-shot ``on_press`` / ``on_release`` triggers instead."""

    hold: Action = None
    on_press: Action = None
    on_release: Action = None
    spec: object = ""               # text, or {"press": ..., "release": ...}

    @property
    def uses_keys(self):
        return any(a is not None and a.kind == "keys" for a in (self.hold, self.on_press, self.on_release))


def parse_action(text):
    if not isinstance(text, str):
        raise ProfileError("an action must be text")
    spec = text.strip()
    if not spec:
        raise ProfileError("an action must not be empty")
    lowered = spec.lower()
    if lowered in SIMPLE_ACTIONS:
        return Action("simple", name=lowered, text=lowered)
    head, sep, rest = spec.partition(":")
    head, rest = head.strip().lower(), rest.strip()
    if sep and head == "key":
        names = [token.strip().upper() for token in rest.split("+")]
        codes = []
        for name in names:
            code = E.ecodes.get(name) if name.startswith("KEY_") else None
            if not code:
                raise ProfileError(f"unknown key '{name}'")
            codes.append(code)
        return Action("keys", codes=tuple(codes), text="key:" + "+".join(names))
    if sep and head == "exec":
        try:
            argv = tuple(shlex.split(rest))
        except ValueError as exc:
            raise ProfileError(f"bad command line: {exc}") from exc
        if not argv:
            raise ProfileError("exec needs a command")
        return Action("exec", argv=argv, text="exec:" + rest)
    if sep and head == "profile":
        if not NAME_PATTERN.match(rest):
            raise ProfileError(f"bad profile name '{rest}'")
        return Action("profile", name=rest, text=f"profile:{rest}")
    raise ProfileError(f"unknown action '{spec}'")


def parse_binding(spec):
    if isinstance(spec, str):
        action = parse_action(spec)
        return Binding(hold=action, spec=action.text)
    if isinstance(spec, dict):
        unknown = set(spec) - {"press", "release"}
        if unknown:
            raise ProfileError(f"unknown field '{sorted(unknown)[0]}' (use press and release)")
        press = parse_action(spec["press"]) if "press" in spec else None
        release = parse_action(spec["release"]) if "release" in spec else None
        if press is None and release is None:
            raise ProfileError("give at least one of press or release")
        canonical = {}
        if press:
            canonical["press"] = press.text
        if release:
            canonical["release"] = release.text
        return Binding(on_press=press, on_release=release, spec=canonical)
    raise ProfileError("a button needs an action text or a {press, release} table")


BUILTIN = {
    "default": (
        "Mouse and scrolling.",
        {"b": "pointer.hold", "a": "scroll.hold", "one": "mouse.left", "two": "mouse.right",
         "plus": "sensitivity.up", "minus": "sensitivity.down", "home": "calibrate",
         "up": "scroll.up", "down": "scroll.down", "left": "scroll.left", "right": "scroll.right"},
    ),
    "presentation": (
        "Slides: D-pad and A change slides, 2 is Escape, B is still a laser-pointer cursor.",
        {"a": "key:KEY_RIGHT", "up": "key:KEY_PAGEUP", "down": "key:KEY_PAGEDOWN",
         "left": "key:KEY_LEFT", "right": "key:KEY_RIGHT", "two": "key:KEY_ESC"},
    ),
    "media": (
        "Media remote: A plays/pauses, D-pad changes volume and tracks, 2 mutes.",
        {"a": "key:KEY_PLAYPAUSE", "up": "key:KEY_VOLUMEUP", "down": "key:KEY_VOLUMEDOWN",
         "left": "key:KEY_PREVIOUSSONG", "right": "key:KEY_NEXTSONG", "two": "key:KEY_MUTE"},
    ),
}


@dataclass
class Profile:
    name: str
    description: str
    source: str                     # "builtin", "config" or "ui": the layer defining it
    builtin: bool                   # the name is one of the built-in profiles
    modified: bool                  # a built-in profile changed by config or the UI
    own: dict                       # button -> spec this profile defines itself
    bindings: dict = field(default_factory=dict)    # button -> Binding, with inheritance

    @property
    def uses_keys(self):
        return any(binding.uses_keys for binding in self.bindings.values())

    def public(self):
        return {"name": self.name, "description": self.description, "source": self.source,
                "builtin": self.builtin, "modified": self.modified, "own": sorted(self.own),
                "buttons": {button: self.bindings[button].spec for button in BUTTONS
                            if button in self.bindings}}


def clean_definition(definition):
    """Validate a profile definition as sent by a client. Returns a normalized dict."""
    if not isinstance(definition, dict):
        raise ProfileError("a profile must be an object")
    unknown = set(definition) - {"description", "buttons"}
    if unknown:
        raise ProfileError(f"unknown field '{sorted(unknown)[0]}'")
    description = definition.get("description", "")
    if not isinstance(description, str):
        raise ProfileError("description must be text")
    buttons = definition.get("buttons", {})
    if not isinstance(buttons, dict):
        raise ProfileError("buttons must be an object")
    cleaned = {}
    for button, spec in buttons.items():
        if button not in BUTTONS:
            raise ProfileError(f"unknown button '{button}' (use {', '.join(BUTTONS)})")
        cleaned[button] = parse_binding(spec).spec
    return {"description": description, "buttons": cleaned}


def build(config_profiles, ui_profiles):
    """Combine built-in, config.toml and UI-saved profiles. Returns (profiles, warnings)."""
    warnings = []
    definitions = {}
    for name, (description, buttons) in BUILTIN.items():
        definitions[name] = {"description": description, "own": dict(buttons),
                             "source": "builtin", "builtin": True, "modified": False}
    for layer, source in ((config_profiles, "config"), (ui_profiles, "ui")):
        for name, table in layer.items():
            where = f"profile '{name}'"
            if not NAME_PATTERN.match(str(name)):
                warnings.append(f"{where}: names use lowercase letters, digits, - and _; ignored.")
                continue
            if not isinstance(table, dict):
                warnings.append(f"{where}: should be a table; ignored.")
                continue
            entry = definitions.setdefault(name, {"description": "", "own": {}, "source": source,
                                                  "builtin": False, "modified": False})
            if entry["builtin"]:
                entry["modified"] = True
            else:
                entry["source"] = source
            if isinstance(table.get("description"), str) and table["description"]:
                entry["description"] = table["description"]
            buttons = table.get("buttons", {})
            if not isinstance(buttons, dict):
                warnings.append(f"{where}: 'buttons' should be a table; ignored.")
                continue
            for button, spec in buttons.items():
                if button not in BUTTONS:
                    warnings.append(f"{where}: unknown button '{button}'; ignored.")
                    continue
                try:
                    parse_binding(spec)
                except ProfileError as exc:
                    warnings.append(f"{where}, button '{button}': {exc}; ignored.")
                    continue
                entry["own"][button] = spec

    base = {button: parse_binding(spec) for button, spec in definitions["default"]["own"].items()}
    profiles = {}
    for name, entry in definitions.items():
        bindings = dict(base)
        for button, spec in entry["own"].items():
            bindings[button] = parse_binding(spec)
        profiles[name] = Profile(name=name, description=entry["description"], source=entry["source"],
                                 builtin=entry["builtin"], modified=entry["modified"],
                                 own=dict(entry["own"]), bindings=bindings)
    return profiles, warnings


class ProfileManager:
    """All profiles plus which one is active."""

    def __init__(self):
        self.active = "default"
        self.profiles, self.warnings = build({}, {})

    def rebuild(self, config_profiles, ui_profiles):
        self.profiles, self.warnings = build(config_profiles, ui_profiles)
        if self.active not in self.profiles:
            self.active = "default"
        return self.warnings

    def names(self):
        builtin = [name for name in BUILTIN if name in self.profiles]
        return builtin + sorted(name for name in self.profiles if name not in BUILTIN)

    def get(self, name=None):
        return self.profiles[name or self.active]

    def activate(self, name):
        if name not in self.profiles:
            raise KeyError(name)
        self.active = name

    def cycle(self, step):
        names = self.names()
        self.active = names[(names.index(self.active) + step) % len(names)]
        return self.active

    def binding(self, button):
        return self.profiles[self.active].bindings.get(button)

    def public(self, name=None):
        data = self.get(name).public()
        data["active"] = data["name"] == self.active
        return data
