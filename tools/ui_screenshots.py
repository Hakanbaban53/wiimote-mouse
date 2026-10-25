"""Run the UI against a daemon with fake devices and save a PNG of every page.

Development aid (also handy for README images). Needs a display, for example:

    xvfb-run -a -s "-screen 0 1400x1000x24" python3 tools/ui_screenshots.py /tmp/shots
"""

import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("GSK_RENDERER", "cairo")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402
from evdev import ecodes as E  # noqa: E402

from tests.helpers import DaemonCase, ev, key  # noqa: E402
from wiimote_mouse import dsp, settings  # noqa: E402
from wiimote_mouse.ui.app import Window  # noqa: E402


def pump(seconds):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def snapshot(widget, path):
    paintable = Gtk.WidgetPaintable.new(widget)
    width, height = widget.get_width(), widget.get_height()
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, width, height)
    node = snap.to_node()
    texture = widget.get_native().get_renderer().render_texture(node, None)
    texture.save_to_png(str(path))
    print("saved", path, f"{width}x{height}")


class Shots(DaemonCase):
    serve = True

    def runTest(self):
        out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots")
        out.mkdir(parents=True, exist_ok=True)
        dark = len(sys.argv) > 2 and sys.argv[2] == "dark"
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
        settings.IDLE_SLEEP_SECONDS = 0
        buttons, gyro, accel = self.connected("/g1", "/a1", nunchuk="/n1")
        self.calibrate(gyro)
        app = Adw.Application(application_id="dev.wiimotemouse.Shots", flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        win = Window(app, str(self.socket_path))
        win.set_default_size(1000, 760)
        win.present()
        pump(0.8)

        angle = math.radians(28)
        buttons.push(key(E.BTN_SOUTH, 1), key(E.KEY_UP, 1), key(E.BTN_EAST, 1))
        for i in range(40):
            ts = 7000.0 + i * 0.01
            accel.push(ev(E.EV_ABS, E.ABS_RX, 100 * math.sin(angle), ts), ev(E.EV_ABS, E.ABS_RY, 0, ts),
                       ev(E.EV_ABS, E.ABS_RZ, 100 * math.cos(angle), ts), ev(E.EV_SYN, E.SYN_REPORT, 0, ts))
            gyro.push(ev(E.EV_ABS, dsp.X_AXIS, self.controller.center[dsp.X_AXIS] + 38 * settings.GYRO_UNITS_PER_DEGREE_S, ts),
                      ev(E.EV_ABS, dsp.Y_AXIS, self.controller.center[dsp.Y_AXIS] - 15 * settings.GYRO_UNITS_PER_DEGREE_S, ts),
                      ev(E.EV_SYN, E.SYN_REPORT, 0, ts))
            pump(0.01)
        self.world.devices["/n1"].push(ev(E.EV_ABS, E.ABS_HAT0X, 70), ev(E.EV_ABS, E.ABS_HAT0Y, 40),
                                       ev(E.EV_KEY, E.BTN_C, 1), ev(E.EV_SYN, E.SYN_REPORT, 0))
        pump(0.6)
        snapshot(win, out / "remote.png")
        for name in ("settings", "profiles", "diagnostics"):
            win.stack.set_visible_child_name(name)
            pump(1.2 if name == "diagnostics" else 0.5)
            snapshot(win, out / f"{name}.png")
        win.stack.set_visible_child_name("remote")
        self.daemon.server.close()
        pump(1.5)
        snapshot(win, out / "down.png")
        win.link.stop()
        self.shutdown.request()
        self.thread.join(3)


if __name__ == "__main__":
    case = Shots()
    case.setUp()
    try:
        case.runTest()
    finally:
        case.doCleanups()
