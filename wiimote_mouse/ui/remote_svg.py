"""Vector drawings of the remote, its tilt gauge and the Nunchuk, generated from live state.

Pure Python producing SVG text: the GTK UI loads it as an image, and any other front end
(a web page, a Tauri app) could embed the same strings. All shapes are original artwork.
"""

from xml.sax.saxutils import escape

PALETTES = {
    "light": {"body": "#e9e9ec", "edge": "#9a9aa3", "key": "#fafafc", "key_edge": "#8d8d96", "text": "#3d3d44",
              "accent": "#3584e4", "accent_text": "#ffffff", "led_off": "#c8c8d0", "led_on": "#2ec27e",
              "dim": "#b5b5be", "ring": "#d6d6dd"},
    "dark": {"body": "#2f2f35", "edge": "#6d6d78", "key": "#46464e", "key_edge": "#7d7d88", "text": "#e6e6ea",
             "accent": "#62a0ea", "accent_text": "#0b1b30", "led_off": "#4a4a52", "led_on": "#57e389",
             "dim": "#5a5a64", "ring": "#44444c"},
}

DPAD_ARROWS = {   # button -> triangle points
    "up": "80,68 91,84 69,84", "down": "80,132 91,116 69,116",
    "left": "48,100 64,89 64,111", "right": "112,100 96,89 96,111",
}
ROUND_BUTTONS = {   # button -> (cx, cy, r, label)
    "a": (80, 190, 21, "A"), "minus": (44, 252, 9, "\u2212"), "home": (80, 252, 11, ""),
    "plus": (116, 252, 9, "+"), "one": (80, 330, 14, "1"), "two": (80, 372, 14, "2"),
}


def _palette(dark):
    return PALETTES["dark" if dark else "light"]


def _round_button(button, pressed, p):
    cx, cy, r, label = ROUND_BUTTONS[button]
    on = button in pressed
    fill, stroke = (p["accent"], p["accent"]) if on else (p["key"], p["key_edge"])
    ink = p["accent_text"] if on else p["text"]
    parts = [f'<circle id="btn-{button}" class="{"on" if on else "off"}" cx="{cx}" cy="{cy}" r="{r}" '
             f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>']
    if button == "home":
        parts.append(f'<path d="M{cx - 6},{cy + 1} L{cx},{cy - 5} L{cx + 6},{cy + 1} M{cx - 4},{cy} V{cy + 5} '
                     f'H{cx + 4} V{cy}" fill="none" stroke="{ink}" stroke-width="1.6" stroke-linejoin="round"/>')
    elif label:
        size = 15 if r > 12 else 13
        parts.append(f'<text x="{cx}" y="{cy + size * 0.36:.1f}" font-size="{size}" font-weight="700" '
                     f'text-anchor="middle" fill="{ink}" font-family="sans-serif">{escape(label)}</text>')
    return "".join(parts)


def render_remote(view, dark=False):
    """The remote with pressed buttons highlighted and the LEDs as they are lit."""
    p = _palette(dark)
    pressed = view.get("pressed", set())
    leds = view.get("leds") or 0
    opacity = 1.0 if view.get("connected") else 0.4
    if view.get("asleep"):
        opacity = min(opacity, 0.7)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 170 520" width="170" height="520">',
           f'<g id="remote" opacity="{opacity}">',
           f'<rect x="20" y="8" width="120" height="504" rx="24" fill="{p["body"]}" stroke="{p["edge"]}" stroke-width="2"/>',
           f'<circle cx="80" cy="36" r="9" fill="none" stroke="{p["edge"]}" stroke-width="2"/>',
           f'<line x1="80" y1="30" x2="80" y2="37" stroke="{p["edge"]}" stroke-width="2" stroke-linecap="round"/>']
    # D-pad: a plus-shaped plate, then one arrow per direction
    out.append(f'<path d="M68,60 h24 a4,4 0 0 1 4,4 v24 h24 a4,4 0 0 1 4,4 v16 a4,4 0 0 1 -4,4 h-24 v24 '
               f'a4,4 0 0 1 -4,4 h-24 a4,4 0 0 1 -4,-4 v-24 h-24 a4,4 0 0 1 -4,-4 v-16 a4,4 0 0 1 4,-4 h24 v-24 '
               f'a4,4 0 0 1 4,-4z" fill="{p["key"]}" stroke="{p["key_edge"]}" stroke-width="1.5"/>')
    for name, points in DPAD_ARROWS.items():
        on = name in pressed
        out.append(f'<polygon id="btn-{name}" class="{"on" if on else "off"}" points="{points}" '
                   f'fill="{p["accent"] if on else p["dim"]}"/>')
    for name in ("a", "minus", "home", "plus", "one", "two"):
        out.append(_round_button(name, pressed, p))
    # speaker grille, purely decorative
    for row in range(4):
        for col in range(5):
            out.append(f'<circle cx="{58 + col * 11}" cy="{430 + row * 10}" r="2" fill="{p["dim"]}"/>')
    # player LEDs
    for index in range(4):
        on = bool(leds & (1 << index))
        out.append(f'<rect id="led-{index + 1}" class="{"on" if on else "off"}" x="{44 + index * 21}" y="490" '
                   f'width="13" height="7" rx="3" fill="{p["led_on"] if on else p["led_off"]}"/>')
    out.append("</g>")
    # B is the trigger on the back: drawn as a tab on the side
    on = "b" in pressed
    out.append(f'<g id="b-trigger" opacity="{opacity}"><rect id="btn-b" class="{"on" if on else "off"}" x="140" y="150" '
               f'width="22" height="64" rx="8" fill="{p["accent"] if on else p["key"]}" '
               f'stroke="{p["accent"] if on else p["key_edge"]}" stroke-width="1.5"/>'
               f'<text x="151" y="188" font-size="14" font-weight="700" text-anchor="middle" '
               f'fill="{p["accent_text"] if on else p["text"]}" font-family="sans-serif">B</text></g>')
    out.append("</svg>")
    return "".join(out)


def _clamp(value, low, high):
    return max(low, min(high, value))


def render_gauges(motion, calibrating=False, dark=False):
    """A tilt (roll) indicator with the yaw/pitch rotation rates and the clutch state."""
    p = _palette(dark)
    roll = None if motion is None else motion.get("roll")
    yaw = None if motion is None else motion.get("yaw")
    pitch = None if motion is None else motion.get("pitch")
    cx, cy, r = 110, 76, 56
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 262" width="220" height="262">',
           f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{p["body"]}" stroke="{p["edge"]}" stroke-width="2"/>']
    for angle in range(-90, 91, 30):
        out.append(f'<line x1="{cx}" y1="{cy - r + 3}" x2="{cx}" y2="{cy - r + 9}" stroke="{p["dim"]}" stroke-width="2" '
                   f'transform="rotate({angle} {cx} {cy})"/>')
    if roll is None:
        out.append(f'<text id="roll-text" x="{cx}" y="{cy + 5}" font-size="13" text-anchor="middle" fill="{p["dim"]}" '
                   f'font-family="sans-serif">no tilt data</text>')
    else:
        out.append(f'<g id="horizon" transform="rotate({_clamp(roll, -180, 180):.1f} {cx} {cy})">'
                   f'<line x1="{cx - r + 8}" y1="{cy}" x2="{cx + r - 8}" y2="{cy}" stroke="{p["accent"]}" stroke-width="4" '
                   f'stroke-linecap="round"/><circle cx="{cx}" cy="{cy}" r="5" fill="{p["accent"]}"/></g>')
        out.append(f'<text id="roll-text" x="{cx}" y="{cy + r + 18}" font-size="13" text-anchor="middle" fill="{p["text"]}" '
                   f'font-family="sans-serif">roll {roll:+.0f}\u00b0</text>')
    for index, (label, value) in enumerate((("yaw", yaw), ("pitch", pitch))):
        y = 184 + index * 28
        out.append(f'<text x="8" y="{y + 4}" font-size="11" fill="{p["text"]}" font-family="sans-serif">{label}</text>')
        out.append(f'<rect x="44" y="{y - 6}" width="120" height="12" rx="6" fill="{p["ring"]}"/>')
        out.append(f'<line x1="104" y1="{y - 8}" x2="104" y2="{y + 8}" stroke="{p["dim"]}" stroke-width="1"/>')
        if value is not None:
            half = _clamp(value / 200.0, -1, 1) * 58
            x = 104 + min(half, 0)
            out.append(f'<rect id="bar-{label}" x="{x:.1f}" y="{y - 6}" width="{abs(half):.1f}" height="12" rx="3" '
                       f'fill="{p["accent"]}"/>')
        text = "n/a" if value is None else f"{value:+.0f}\u00b0/s"
        out.append(f'<text x="212" y="{y + 4}" font-size="11" text-anchor="end" fill="{p["text"]}" '
                   f'font-family="sans-serif">{text}</text>')
    clutch = bool(motion and motion.get("clutch"))
    scroll = bool(motion and motion.get("scroll"))
    for index, (label, on) in enumerate((("MOVE", clutch), ("SCROLL", scroll))):
        x = 24 + index * 94
        out.append(f'<rect id="pill-{label.lower()}" class="{"on" if on else "off"}" x="{x}" y="234" width="80" height="20" '
                   f'rx="10" fill="{p["accent"] if on else p["ring"]}"/>'
                   f'<text x="{x + 40}" y="248" font-size="11" font-weight="700" text-anchor="middle" '
                   f'fill="{p["accent_text"] if on else p["dim"]}" font-family="sans-serif">{label}</text>')
    if calibrating:
        out.append(f'<text x="{cx}" y="11" font-size="11" text-anchor="middle" fill="{p["accent"]}" '
                   f'font-family="sans-serif">calibrating</text>')
    out.append("</svg>")
    return "".join(out)


NUNCHUK_STICK_RANGE = 120.0      # hid-wiimote reports the Nunchuk stick as roughly -120..120


def render_nunchuk(values, dark=False):
    """The Nunchuk: stick position and the C and Z buttons, from the "ext" event values."""
    p = _palette(dark)
    values = values or {}
    sx = _clamp(values.get("ABS_HAT0X", 0) / NUNCHUK_STICK_RANGE, -1, 1)
    sy = _clamp(values.get("ABS_HAT0Y", 0) / NUNCHUK_STICK_RANGE, -1, 1)
    c_on, z_on = bool(values.get("BTN_C")), bool(values.get("BTN_Z"))
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 170 290" width="170" height="290">',
           f'<path d="M45,10 Q85,-4 125,10 Q150,70 140,150 Q132,230 108,276 Q85,290 62,276 Q38,230 30,150 Q20,70 45,10z" '
           f'fill="{p["body"]}" stroke="{p["edge"]}" stroke-width="2"/>',
           f'<circle cx="85" cy="70" r="38" fill="{p["ring"]}" stroke="{p["key_edge"]}" stroke-width="1.5"/>',
           f'<circle id="stick" cx="{85 + sx * 22:.1f}" cy="{70 + sy * -22:.1f}" r="17" fill="{p["accent"] if (sx or sy) else p["key"]}" '
           f'stroke="{p["key_edge"]}" stroke-width="1.5"/>']
    for name, y, on in (("c", 150, c_on), ("z", 196, z_on)):
        out.append(f'<rect id="btn-{name}" class="{"on" if on else "off"}" x="58" y="{y}" width="54" height="34" rx="14" '
                   f'fill="{p["accent"] if on else p["key"]}" stroke="{p["accent"] if on else p["key_edge"]}" stroke-width="1.5"/>'
                   f'<text x="85" y="{y + 23}" font-size="17" font-weight="700" text-anchor="middle" '
                   f'fill="{p["accent_text"] if on else p["text"]}" font-family="sans-serif">{name.upper()}</text>')
    out.append("</svg>")
    return "".join(out)
