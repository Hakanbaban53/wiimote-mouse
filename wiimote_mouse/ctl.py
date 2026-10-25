"""wiimotectl: control the running wiimote-mouse daemon from the command line.

Exit codes: 0 success, 1 the daemon rejected the command, 2 usage error,
3 the daemon cannot be reached.
"""

import argparse
import json
import sys

from . import __version__, doctor, settings
from .client import Client, ClientError, ConnectionFailed
from .control import TOPICS

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_UNREACHABLE = 0, 1, 2, 3

MODE_TEXT = {
    "ready": "ready",
    "calibrating": "calibrating (hold the remote still)",
    "waiting": "waiting for the MotionPlus",
    "asleep": "sensors asleep (press any button)",
}


def out(text=""):
    print(text, flush=True)


def duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m" if hours else (f"{minutes}m {secs:02d}s" if minutes else f"{secs}s")


def show(args, data, human):
    if args.json:
        out(json.dumps(data, indent=2, sort_keys=True))
    else:
        human(data)


def parse_value(text):
    """Config values are true/false or numbers, written the way JSON writes them."""
    try:
        value = json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not true, false or a number") from None
    if isinstance(value, (str, list, dict)) or value is None:
        raise argparse.ArgumentTypeError(f"'{text}' is not true, false or a number")
    return value


def parse_spec(text):
    """A button action: plain text, or a JSON {"press": ..., "release": ...} table."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"bad JSON table: {exc}") from None
    return text


def parse_assignment(text):
    button, sep, spec = text.partition("=")
    if not sep or not button:
        raise argparse.ArgumentTypeError(f"'{text}' should look like BUTTON=ACTION")
    return button.strip(), parse_spec(spec)


# ---------------------------------------------------------------- commands

def cmd_status(client, args):
    def human(status):
        daemon = status["daemon"]
        remote = status["remote"]
        out(f"Daemon       v{daemon['version']}, up {duration(daemon['uptime'])}")
        if remote is None:
            out("Remote       not connected")
        else:
            out(f"Remote       connected ({remote['id']})")
            out(f"State        {MODE_TEXT.get(remote['mode'], remote['mode'] or 'starting')}")
            if remote["battery"] is not None:
                out(f"Battery      {remote['battery']}%")
            sensors = remote["sensors"]
            out("Sensors      gyro " + ("on" if sensors["gyro"] else "off")
                + ", accelerometer " + ("on" if sensors["accel"] else "off"))
            out("Extensions  " + (", ".join(remote["extensions"]) or "none"))
        out(f"Profile      {status['profile']['active']}")
        out(f"Sensitivity  {status['pointer']['sensitivity']}")

    show(args, client.call("status"), human)
    return EXIT_OK


def cmd_calibrate(client, args):
    client.call("calibrate")
    out("Calibrating: hold the remote still for about half a second.")
    return EXIT_OK


def cmd_sensitivity(client, args):
    status = client.call("status")
    current = status["pointer"]["sensitivity"]
    if args.value is None:
        show(args, {"sensitivity": current}, lambda d: out(f"{d['sensitivity']}"))
        return EXIT_OK
    if args.value in ("up", "down"):
        target = current * (1.12 if args.value == "up" else 1 / 1.12)
    else:
        try:
            target = float(args.value)
        except ValueError:
            raise ClientError("sensitivity takes a number, 'up' or 'down'") from None
    result = client.call("config.set", key="pointer.sensitivity", value=round(target, 2))
    show(args, result, lambda d: out(f"Sensitivity: {d['value']}"))
    return EXIT_OK


def cmd_config(client, args):
    action = args.action
    if action == "list":
        result = client.call("config.get")

        def human(data):
            rows = data["options"]
            width = max(len(row["key"]) for row in rows)
            for row in rows:
                value = str(row["value"]).lower() if isinstance(row["value"], bool) else row["value"]
                out(f"{row['key']:<{width}}  {value!s:<7} {row['source']:<8} {row['description']}")

        show(args, result, human)
    elif action == "get":
        result = client.call("config.get", key=args.key)
        show(args, result, lambda d: out(str(d["value"]).lower() if isinstance(d["value"], bool) else d["value"]))
    elif action == "set":
        result = client.call("config.set", key=args.key, value=args.value)

        def human(d):
            shown = str(d["value"]).lower() if isinstance(d["value"], bool) else d["value"]
            out(f"{d['key']} = {shown} ({d['source']})")
            if d["restart_required"]:
                out("This change takes effect after restarting the daemon.")

        show(args, result, human)
    elif action == "reset":
        result = client.call("config.reset", key=args.key)
        show(args, result, lambda d: out(f"{d['key']} = {d['value']} ({d['source']})"))
    else:   # reload
        result = client.call("config.reload")
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        show(args, result, lambda d: out("Configuration reloaded."))
    return EXIT_OK


def print_profile(profile):
    marker = "  (active)" if profile.get("active") else ""
    out(f"{profile['name']}{marker}  [{profile['source']}{', modified' if profile['modified'] else ''}]")
    if profile["description"]:
        out(f"  {profile['description']}")
    for button, spec in profile["buttons"].items():
        own = "*" if button in profile["own"] else " "
        text = spec if isinstance(spec, str) else ", ".join(f"on {k}: {v}" for k, v in spec.items())
        out(f"  {own} {button:<6} {text}")


def cmd_profile(client, args):
    action = args.action
    if action == "list":
        result = client.call("profile.list")

        def human(data):
            for profile in data["profiles"]:
                star = "*" if profile["active"] else " "
                out(f"{star} {profile['name']:<14} {profile['description']}")

        show(args, result, human)
    elif action == "show":
        args_ = {"name": args.name} if args.name else {}
        show(args, client.call("profile.get", **args_), print_profile)
    elif action == "set":
        show(args, client.call("profile.set", name=args.name), lambda d: out(f"Active profile: {d['name']}"))
    elif action in ("next", "prev"):
        result = client.call("profile.cycle", delta=1 if action == "next" else -1)
        show(args, result, lambda d: out(f"Active profile: {d['name']}"))
    elif action == "save":
        definition = {"description": args.description or "", "buttons": dict(args.button or [])}
        show(args, client.call("profile.save", name=args.name, profile=definition),
             lambda d: out(f"Saved profile '{d['name']}'."))
    elif action in ("bind", "unbind"):
        try:
            current = client.call("profile.get", name=args.name).get("ui")
        except ClientError:
            current = None                      # a brand new profile
        definition = current or {"description": "", "buttons": {}}
        if action == "bind":
            definition["buttons"][args.button] = args.spec
        elif args.button not in definition["buttons"]:
            raise ClientError(f"'{args.button}' has no saved binding in '{args.name}'")
        else:
            del definition["buttons"][args.button]
        if action == "unbind" and not definition["buttons"] and not definition["description"]:
            client.call("profile.delete", name=args.name)
            out(f"Removed the saved changes to '{args.name}'.")
        else:
            show(args, client.call("profile.save", name=args.name, profile=definition),
                 lambda d: out(f"Saved '{d['name']}': {args.button} -> "
                               f"{d['buttons'].get(args.button, 'inherited')}"))
    else:   # delete
        show(args, client.call("profile.delete", name=args.name), lambda d: out(f"Deleted '{d['deleted']}'."))
    return EXIT_OK


def cmd_rumble(client, args):
    client.call("rumble", count=args.count)
    return EXIT_OK


def cmd_led(client, args):
    mask = None if args.mask == "auto" else args.mask
    show(args, client.call("led", mask=mask), lambda d: out(f"LEDs: {d['leds']:04b}"))
    return EXIT_OK


def cmd_sensors(client, args):
    show(args, client.call("sensors", action=args.action),
         lambda d: out("Sensors asleep." if d["asleep"] else "Sensors awake."))
    return EXIT_OK


def cmd_devices(client, args):
    def human(data):
        if not data["remotes"]:
            out("No Wii Remote input devices found.")
        for remote, paths in data["remotes"].items():
            out(remote)
            for slot, path in sorted(paths.items()):
                out(f"  {slot:<15} {path}")

    show(args, client.call("devices"), human)
    return EXIT_OK


def describe_event(topic, data):
    if topic == "state":
        remote = data["remote"] or {}
        return (f"state    connected={data['connected']} mode={remote.get('mode')} "
                f"battery={remote.get('battery')} profile={data['profile']['active']} "
                f"extensions={','.join(remote.get('extensions', [])) or '-'}")
    if topic == "buttons":
        return f"button   {data['button']} {'down' if data['pressed'] else 'up'}"
    if topic == "motion":
        return (f"motion   yaw={data['yaw']} pitch={data['pitch']} dx={data['dx']} dy={data['dy']} "
                f"roll={data['roll']} calibrating={data['calibrating']}")
    values = " ".join(f"{k}={v}" for k, v in sorted(data["values"].items()))
    return f"ext      {data['device']} {values}"


def cmd_watch(client, args):
    topics = args.topics or list(TOPICS)
    result = client.subscribe(topics)
    seen = 0

    def emit(topic, data):
        out(json.dumps({"event": topic, "data": data}) if args.json else describe_event(topic, data))

    if "state" in topics:
        emit("state", result["state"])
        seen += 1
    while args.count is None or seen < args.count:
        event = client.next_event(timeout=args.timeout)
        if event is None:
            break
        emit(*event)
        seen += 1
    return EXIT_OK


def cmd_raw(client, args):
    try:
        payload = json.loads(args.json_args) if args.json_args else {}
    except ValueError as exc:
        raise ClientError(f"arguments must be a JSON object: {exc}") from None
    if not isinstance(payload, dict):
        raise ClientError("arguments must be a JSON object")
    out(json.dumps(client.call(args.command_name, **payload), indent=2, sort_keys=True))
    return EXIT_OK


def cmd_doctor(client, args):
    return doctor.run(settings.default_config_path())


def cmd_ui(client, args):
    """Open the graphical UI (it connects to the daemon itself)."""
    from .ui import launch
    return launch.main(["--socket", args.socket] if args.socket else [])


# ---------------------------------------------------------------- parser

def build_parser():
    parser = argparse.ArgumentParser(
        prog="wiimotectl", description="Control the running wiimote-mouse daemon.",
        epilog="Run 'wiimotectl COMMAND --help' for details on a command.")
    parser.add_argument("--socket", help="control socket path (default: $XDG_RUNTIME_DIR/wiimote-mouse/control.sock)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version", version=f"wiimotectl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name, func, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    command("status", cmd_status, "show the daemon, the remote and the active profile")
    command("calibrate", cmd_calibrate, "recalibrate the gyro (hold the remote still)")

    p = command("sensitivity", cmd_sensitivity, "show or change the pointer speed")
    p.add_argument("value", nargs="?", help="a number, 'up' or 'down'; omit to show the current value")

    p = command("config", cmd_config, "list, read, change or reload settings")
    csub = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    csub.add_parser("list", help="every setting with its value and source")
    csub.add_parser("get", help="print one setting").add_argument("key")
    s = csub.add_parser("set", help="change a setting now and remember it")
    s.add_argument("key")
    s.add_argument("value", type=parse_value)
    csub.add_parser("reset", help="drop a saved change").add_argument("key")
    csub.add_parser("reload", help="re-read config.toml")

    p = command("profile", cmd_profile, "list and switch button profiles, or edit them")
    psub = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    psub.add_parser("list", help="all profiles")
    psub.add_parser("show", help="the buttons of a profile").add_argument("name", nargs="?")
    psub.add_parser("set", help="activate a profile").add_argument("name")
    psub.add_parser("next", help="activate the next profile")
    psub.add_parser("prev", help="activate the previous profile")
    s = psub.add_parser("save", help="create or replace a saved profile")
    s.add_argument("name")
    s.add_argument("--description")
    s.add_argument("--button", action="append", type=parse_assignment, metavar="BUTTON=ACTION")
    s = psub.add_parser("bind", help="change one button of a profile")
    s.add_argument("name")
    s.add_argument("button")
    s.add_argument("spec", type=parse_spec, metavar="ACTION")
    s = psub.add_parser("unbind", help="drop a saved button change")
    s.add_argument("name")
    s.add_argument("button")
    psub.add_parser("delete", help="delete a saved profile").add_argument("name")

    p = command("rumble", cmd_rumble, "buzz the remote")
    p.add_argument("count", nargs="?", type=int, default=1)
    p = command("led", cmd_led, "show an LED pattern (test) or go back to automatic")
    p.add_argument("mask", help="0-15 (bit 0 is LED 1), or 'auto'", type=lambda t: t if t == "auto" else int(t))
    p = command("sensors", cmd_sensors, "put the sensors to sleep or wake them")
    p.add_argument("action", choices=("sleep", "wake"))
    command("devices", cmd_devices, "list the remote's input devices")
    p = command("watch", cmd_watch, "stream live events until interrupted")
    p.add_argument("--topics", nargs="+", choices=TOPICS, help="topics to follow (default: all)")
    p.add_argument("--count", type=int, help="stop after this many events")
    p.add_argument("--timeout", type=float, help="stop after this many seconds without an event")
    p = command("raw", cmd_raw, "send a raw command (for scripts)")
    p.add_argument("command_name", metavar="NAME")
    p.add_argument("json_args", nargs="?", metavar="JSON")
    command("doctor", cmd_doctor, "check the installation (runs locally, daemon not needed)")
    command("ui", cmd_ui, "open the graphical UI (same as wiimote-mouse-ui)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command in ("doctor", "ui"):
        return args.func(None, args)
    try:
        with Client(args.socket) as client:
            return args.func(client, args)
    except ConnectionFailed as exc:
        print(f"wiimotectl: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except ClientError as exc:
        print(f"wiimotectl: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
