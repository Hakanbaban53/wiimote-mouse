"""wiimote-mouse-ui: a GTK 4 + libadwaita front end for the daemon.

The UI only talks to the daemon's control socket, so the daemon keeps working when it is closed,
and the UI costs nothing while it is not open. Expensive streams (motion, extension input) are
requested only while the page that shows them is actually visible.
"""

import argparse
import json
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from .. import __version__, doctor, profiles, settings  # noqa: E402
from . import remote_svg  # noqa: E402
from .link import LOST, NOT_RUNNING, DaemonLink  # noqa: E402
from .model import RemoteModel  # noqa: E402

APP_ID = "dev.wiimotemouse.Ui"

BUTTON_LABELS = {"a": "A", "b": "B (trigger)", "one": "1", "two": "2", "plus": "+", "minus": "\u2212",
                 "home": "Home", "up": "D-pad up", "down": "D-pad down", "left": "D-pad left", "right": "D-pad right"}
SECTION_TITLES = {"pointer": "Pointer", "scroll": "Scrolling", "filter": "Smoothing", "power": "Power",
                  "tilt": "Tilt", "feedback": "Feedback"}
STATUS_ICONS = {doctor.OK: "object-select-symbolic", doctor.WARN: "dialog-warning-symbolic",
                doctor.FAIL: "dialog-error-symbolic"}
STATUS_CLASSES = {doctor.OK: "success", doctor.WARN: "warning", doctor.FAIL: "error"}


def svg_texture(svg, width, height):
    """Render SVG text to a texture (at the given pixel size; GTK scales it to fit)."""
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg.encode("utf-8")))
    pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, width, height, True, None)
    return Gdk.Texture.new_for_pixbuf(pixbuf)


def is_dark():
    return Adw.StyleManager.get_default().get_dark()


LABELS = {
    "pointer.accel_reference_dps": "Acceleration peak speed",
    "scroll.hires": "High-resolution scrolling",
    "scroll.gyro_notches_per_degree": "Gyro scroll speed",
    "filter.min_cutoff": "Minimum cutoff",
    "power.idle_sleep_seconds": "Idle sleep (seconds)",
    "tilt.roll_compensation": "Roll compensation",
    "tilt.roll_invert": "Invert roll correction",
    "feedback.leds": "Player LEDs",
    "feedback.rumble": "Rumble",
    "feedback.notifications": "Desktop notifications",
    "feedback.battery_low_percent": "Low battery warning (%)",
}


def human(key):
    return LABELS.get(key) or key.split(".", 1)[1].replace("_", " ").capitalize()


class RemotePage(Gtk.Box):
    """The live picture of the remote, its state, tilt gauges and any plugged-in extension."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=40, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER, margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        self.window = window
        self._remote_key = self._gauge_key = self._nunchuk_key = None
        self._ext_rows = {}
        self._ext_signature = None

        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.set_size_request(150, 458)
        self.append(self.picture)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER)
        side.set_size_request(360, -1)
        self.append(side)

        group = Adw.PreferencesGroup()
        side.append(group)
        self.status_row = Adw.ActionRow(title="Status")
        self.battery_row = Adw.ActionRow(title="Battery")
        self.battery_bar = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER)
        self.battery_bar.set_size_request(110, -1)
        self.battery_label = Gtk.Label(label="\u2013", width_chars=5, xalign=1)
        self.battery_row.add_suffix(self.battery_bar)
        self.battery_row.add_suffix(self.battery_label)
        self.profile_row = Adw.ActionRow(title="Profile")
        self.speed_row = Adw.ActionRow(title="Pointer speed")
        self.sensors_row = Adw.ActionRow(title="Sensors")
        for row in (self.status_row, self.battery_row, self.profile_row, self.speed_row, self.sensors_row):
            group.add(row)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.START)
        self.calibrate_button = Gtk.Button(label="Calibrate")
        self.calibrate_button.add_css_class("suggested-action")
        self.calibrate_button.connect("clicked", lambda _b: window.command(
            "calibrate", lambda ok, _r: ok and window.toast("Calibrating: hold the remote still")))
        buttons.append(self.calibrate_button)
        side.append(buttons)

        self.gauges = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.gauges.set_size_request(220, 262)
        side.append(self.gauges)

        self.ext_group = Adw.PreferencesGroup(title="Extensions")
        self.ext_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.ext_group.add(self.ext_box)
        side.append(self.ext_group)
        self.refresh()

    def refresh(self):
        model, dark = self.window.model, is_dark()
        view = model.remote_view()
        key = (view["connected"], view["asleep"], frozenset(view["pressed"]), view["leds"], dark)
        if key != self._remote_key:
            self._remote_key = key
            self.picture.set_paintable(svg_texture(remote_svg.render_remote(view, dark), 340, 1040))

        remote = model.remote
        self.status_row.set_subtitle(model.title())
        self.battery_row.set_visible(remote is not None)
        if remote is not None and remote["battery"] is not None:
            self.battery_bar.set_value(remote["battery"])
            self.battery_label.set_label(f"{remote['battery']}%")
        else:
            self.battery_bar.set_value(0)
            self.battery_label.set_label("\u2013")
        active = model.state["profile"]["active"]
        self.profile_row.set_subtitle(active or "\u2013")
        sensitivity = model.state["pointer"]["sensitivity"]
        self.speed_row.set_subtitle("\u2013" if sensitivity is None else f"{sensitivity:g}")
        if remote is None:
            self.sensors_row.set_subtitle("\u2013")
        else:
            sensors = remote["sensors"]
            self.sensors_row.set_subtitle(
                ("asleep" if remote["asleep"] else
                 "MotionPlus " + ("on" if sensors["gyro"] else "off")
                 + ", accelerometer " + ("on" if sensors["accel"] else "off")))
        self.calibrate_button.set_sensitive(remote is not None)
        self.refresh_gauges()
        self.refresh_extensions()

    def refresh_gauges(self):
        model, dark = self.window.model, is_dark()
        motion = model.motion
        calibrating = model.mode == "calibrating"
        key = (None if motion is None else (motion.get("roll"), round(motion.get("yaw") or 0), round(motion.get("pitch") or 0),
                                            motion.get("clutch"), motion.get("scroll")), calibrating, dark)
        if key != self._gauge_key:
            self._gauge_key = key
            self.gauges.set_paintable(svg_texture(remote_svg.render_gauges(motion, calibrating, dark), 440, 524))

    def refresh_extensions(self):
        model, dark = self.window.model, is_dark()
        extensions = model.extensions
        self.ext_group.set_visible(bool(extensions))
        if tuple(extensions) != self._ext_signature:
            self._ext_signature = tuple(extensions)
            child = self.ext_box.get_first_child()
            while child is not None:
                following = child.get_next_sibling()
                self.ext_box.remove(child)
                child = following
            self._ext_rows = {}
            self._nunchuk_key = None
            for name in extensions:
                if name == "nunchuk":
                    picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
                    picture.set_size_request(120, 205)
                    self._ext_rows[name] = picture
                else:
                    self._ext_rows[name] = Adw.ActionRow(title=name.replace("_", " ").capitalize(), subtitle="no input yet")
                self.ext_box.append(self._ext_rows[name])
        for name, widget in self._ext_rows.items():
            values = model.ext.get(name, {})
            if name == "nunchuk":
                key = (tuple(sorted(values.items())), dark)
                if key != self._nunchuk_key:
                    self._nunchuk_key = key
                    widget.set_paintable(svg_texture(remote_svg.render_nunchuk(values, dark), 340, 580))
            elif values:
                widget.set_subtitle("  ".join(f"{k}={v}" for k, v in sorted(values.items())))


class SettingsPage(Adw.PreferencesPage):
    """One row per setting, built from the daemon's own description of its options."""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.rows = {}
        self.options = {}
        self._updating = False

    def load(self, options):
        self._updating = True
        try:
            if not self.rows:
                self._build(options)
            for option in options:
                self._show(option)
        finally:
            self._updating = False

    def _build(self, options):
        groups = {}
        for option in options:
            section = option["key"].split(".", 1)[0]
            if section not in groups:
                groups[section] = Adw.PreferencesGroup(title=SECTION_TITLES.get(section, section.capitalize()))
                self.add(groups[section])
            if option["kind"] == "bool":
                row = Adw.SwitchRow(title=human(option["key"]))
                row.connect("notify::active", self._changed, option["key"])
            else:
                span = option["max"] - option["min"]
                step = 0.001 if span <= 1 else (0.01 if span <= 10 else (0.1 if span <= 100 else 1.0))
                adjustment = Gtk.Adjustment(lower=option["min"], upper=option["max"], step_increment=step,
                                            page_increment=step * 10, value=option["value"])
                row = Adw.SpinRow(title=human(option["key"]), adjustment=adjustment,
                                  digits=3 if step < 0.01 else (2 if step < 0.1 else (1 if step < 1 else 0)))
                row.connect("notify::value", self._changed, option["key"])
            reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Reset to the default")
            reset.add_css_class("flat")
            reset.connect("clicked", lambda _b, key=option["key"]: self.window.command(
                "config.reset", None, key=key, on_done=self._applied))
            row.add_suffix(reset)
            groups[section].add(row)
            self.rows[option["key"]] = (row, reset)

    def _show(self, option):
        row, reset = self.rows[option["key"]]
        self.options[option["key"]] = option
        notes = [option["description"]]
        if not option["live"]:
            notes.append("Needs a restart of the daemon.")
        if option["source"] != "default":
            notes.append(f"Changed ({option['source']}); default is "
                         f"{str(option['default']).lower() if option['kind'] == 'bool' else format(option['default'], 'g')}.")
        row.set_subtitle(" ".join(notes))
        reset.set_visible(option["source"] == "override")
        if option["kind"] == "bool":
            if row.get_active() != option["value"]:
                row.set_active(option["value"])
        elif abs(row.get_value() - option["value"]) > 1e-9:
            row.set_value(option["value"])

    def _changed(self, row, _param, key):
        if self._updating:
            return
        value = row.get_active() if hasattr(row, "get_active") else row.get_value()
        self.window.command("config.set", None, key=key, value=value, on_done=self._applied)

    def _applied(self, ok, result):
        if ok:
            self._updating = True
            try:
                self._show(result)
            finally:
                self._updating = False
            if result.get("restart_required"):
                self.window.toast("Saved. This setting takes effect after restarting the daemon.")

    def sync_sensitivity(self, value):
        """The + / - buttons changed the speed: follow it without echoing it back."""
        if "pointer.sensitivity" in self.rows and value is not None:
            self._updating = True
            try:
                row, _ = self.rows["pointer.sensitivity"]
                if abs(row.get_value() - value) > 1e-6:
                    row.set_value(value)
            finally:
                self._updating = False


class ProfilesPage(Adw.PreferencesPage):
    """Choose the active profile and edit what each button does."""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self._updating = False
        self.profiles = {}
        self.shown = None                       # the profile whose buttons are listed
        self.names = []

        top = Adw.PreferencesGroup(title="Profiles", description="Each profile only lists what differs from 'default'.")
        self.combo = Adw.ComboRow(title="Active profile", model=Gtk.StringList.new([]))
        self.combo.connect("notify::selected", self._selected)
        top.add(self.combo)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.START, margin_top=6)
        new = Gtk.Button(label="New profile\u2026")
        new.connect("clicked", self._new_profile)
        self.delete_button = Gtk.Button(label="Delete my changes", sensitive=False)
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._delete)
        buttons.append(new)
        buttons.append(self.delete_button)
        top.add(buttons)
        self.add(top)

        self.button_group = Adw.PreferencesGroup(
            title="Buttons", description="Edit an action and press Enter. Clear a field to go back to the inherited action.")
        self.entries = {}
        for name in profiles.BUTTONS:
            row = Adw.EntryRow(title=BUTTON_LABELS[name], show_apply_button=True)
            row.connect("apply", self._apply, name)
            row.connect("changed", lambda r: r.remove_css_class("error"))
            self.entries[name] = row
            self.button_group.add(row)
        self.add(self.button_group)

        help_group = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title="Available actions")
        extra = (("key:KEY_LEFTCTRL+KEY_C", "Keyboard keys while the button is held."),
                 ("exec:command args", "Run a command when the button is pressed (no shell)."),
                 ("profile:<name>", "Switch to another profile."),
                 ('{"press": "...", "release": "..."}', "One-shot actions on press and on release."))
        for action, text in (*profiles.SIMPLE_ACTIONS.items(), *extra):
            expander.add_row(Adw.ActionRow(title=action, subtitle=text, use_markup=False))
        help_group.add(expander)
        self.add(help_group)

    # ---- data
    def load(self, listing):
        self.profiles = {p["name"]: p for p in listing["profiles"]}
        self._updating = True
        try:
            names = [p["name"] for p in listing["profiles"]]
            if names != self.names:
                self.names = names
                self.combo.set_model(Gtk.StringList.new(names))
            active = listing["active"]
            self.combo.set_selected(names.index(active))
            self.combo.set_subtitle(self.profiles[active]["description"])
            self.shown = active
            for name, row in self.entries.items():
                self._fill(row, name, self.profiles[active])
        finally:
            self._updating = False

    def _fill(self, row, button, profile):
        spec = profile["buttons"].get(button, "")
        row.set_text(spec if isinstance(spec, str) else json.dumps(spec))
        row.set_title(BUTTON_LABELS[button] + ("" if button in profile["own"] else "  (inherited)"))

    def refresh(self):
        def listed(ok, result):
            if not ok:
                return
            self.load(result)
            self.window.command("profile.get", lambda ok2, profile: ok2 and self._saved_changes(profile),
                                name=result["active"])

        self.window.command("profile.list", listed)

    def _saved_changes(self, profile):
        """Only changes saved from the UI or wiimotectl can be removed here."""
        saved = bool(profile.get("ui"))
        self.delete_button.set_sensitive(saved)
        self.delete_button.set_label("Reset my changes" if profile["builtin"] or profile["source"] == "config"
                                     else "Delete profile")

    # ---- actions
    def _selected(self, combo, _param):
        if self._updating:
            return
        index = combo.get_selected()
        if 0 <= index < len(self.names):
            self.window.command("profile.set", lambda ok, _r: self.refresh(), name=self.names[index])

    def _apply(self, row, button):
        if self._updating or self.shown is None:
            return
        name, text = self.shown, row.get_text().strip()

        def rejected():
            row.add_css_class("error")          # keep what was typed so the typo can be fixed

        def saved(ok, _result):
            if ok:
                self.window.toast("Saved")
                self.refresh()
            else:
                rejected()                      # the daemon's reason was already shown as a toast

        def got(ok, profile):
            if not ok:
                return
            ui = profile.get("ui") or {"description": "", "buttons": {}}
            buttons = dict(ui["buttons"])
            if text:
                try:
                    buttons[button] = json.loads(text) if text.startswith("{") else text
                except ValueError as exc:
                    self.window.toast(f"That is not valid JSON: {exc}")
                    rejected()
                    return
            else:
                buttons.pop(button, None)
            self.window.command("profile.save", saved, name=name,
                                profile={"description": ui["description"], "buttons": buttons})

        self.window.command("profile.get", got, name=name)

    def _delete(self, _button):
        if self.shown is None:
            return
        def removed(ok, _result):
            if ok:
                self.window.toast("Removed")
            self.refresh()

        self.window.command("profile.delete", removed, name=self.shown)

    def _new_profile(self, _button):
        dialog = Adw.AlertDialog.new("New profile", "Use lowercase letters, digits, - and _.")
        entry = Gtk.Entry(placeholder_text="name")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")

        def done(dlg, result):
            if dlg.choose_finish(result) != "create":
                return
            name = entry.get_text().strip()

            def created(ok, _result):
                if ok:
                    self.window.toast(f"Created '{name}'")
                    self.window.command("profile.set", lambda *_: self.refresh(), name=name)

            self.window.command("profile.save", created, name=name, profile={"description": "", "buttons": {}})

        dialog.choose(self.window, None, done)


class DiagnosticsPage(Adw.PreferencesPage):
    """The checks of `wiimote-mouse --doctor`."""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.group = Adw.PreferencesGroup(title="Checks", description="Everything the daemon depends on.")
        self.button = Gtk.Button(label="Run checks", valign=Gtk.Align.CENTER)
        self.button.connect("clicked", lambda _b: self.run())
        self.group.set_header_suffix(self.button)
        self.add(self.group)
        self.rows = []
        self._ran = False
        self._busy = False

    def run(self):
        if self._busy:
            return
        self._busy = True
        self.button.set_sensitive(False)
        self.button.set_label("Checking\u2026")

        def work():
            checks = doctor.collect(settings.default_config_path())      # may block: reads the battery
            GLib.idle_add(self._show, checks)

        threading.Thread(target=work, daemon=True).start()

    def _show(self, checks):
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        for check in checks:
            row = Adw.ActionRow(title=check.text, subtitle=check.hint, use_markup=False)
            icon = Gtk.Image(icon_name=STATUS_ICONS[check.status])
            icon.add_css_class(STATUS_CLASSES[check.status])
            row.add_prefix(icon)
            self.group.add(row)
            self.rows.append(row)
        self._busy = False
        self._ran = True
        self.button.set_sensitive(True)
        self.button.set_label("Run checks")

    def shown(self):
        if not self._ran:
            self.run()


class Window(Adw.ApplicationWindow):
    def __init__(self, app, socket_path=None):
        super().__init__(application=app, title="Wii Remote Mouse", default_width=980, default_height=720)
        self.model = RemoteModel()
        self.link = DaemonLink(socket_path, on_up=self._on_up, on_down=self._on_down, on_event=self._on_event)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        menu = Gio.Menu()
        menu.append("About", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        toolbar.add_top_bar(header)

        self.remote_page = RemotePage(self)
        self.settings_page = SettingsPage(self)
        self.profiles_page = ProfilesPage(self)
        self.diagnostics_page = DiagnosticsPage(self)
        self.stack.add_titled_with_icon(self.remote_page, "remote", "Remote", "input-gaming-symbolic")
        self.stack.add_titled_with_icon(self.settings_page, "settings", "Settings", "preferences-system-symbolic")
        self.stack.add_titled_with_icon(self.profiles_page, "profiles", "Profiles", "view-list-symbolic")
        self.stack.add_titled_with_icon(self.diagnostics_page, "diagnostics", "Diagnostics", "dialog-information-symbolic")

        self.down_page = Adw.StatusPage(icon_name="dialog-warning-symbolic", title="The daemon is not running",
                                        description="Start the wiimote-mouse service to use the remote.")
        start = Gtk.Button(label="Start the service", halign=Gtk.Align.CENTER)
        start.add_css_class("suggested-action")
        start.add_css_class("pill")
        start.connect("clicked", self._start_service)
        self.down_page.set_child(start)

        self.outer = Gtk.Stack()
        self.outer.add_named(self.stack, "main")
        self.outer.add_named(self.down_page, "down")
        self.outer.set_visible_child_name("down")
        toolbar.set_content(self.outer)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        for page in (self.remote_page, self.diagnostics_page):
            page.connect("map", self._page_mapped)
        self.remote_page.connect("unmap", lambda _p: self._update_topics())
        self.stack.connect("notify::visible-child-name", self._page_changed)
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.refresh())
        self.connect("close-request", self._on_close)
        self.link.start()

    # ---- helpers used by the pages
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast.new(text))

    def command(self, cmd, callback=None, on_done=None, **args):
        """Send a command; failures become toasts. ``callback`` (or ``on_done``) gets (ok, result)."""
        handler = callback or on_done

        def finished(ok, result):
            if not ok and result not in (LOST, NOT_RUNNING):
                self.toast(str(result))
            if handler:
                handler(ok, result)

        self.link.call(cmd, finished, **args)

    # ---- daemon link
    def _on_up(self, state):
        self.model.daemon_found(state)
        self.outer.set_visible_child_name("main")
        self.refresh()
        self.command("config.get", lambda ok, r: ok and self.settings_page.load(r["options"]))
        self.profiles_page.refresh()
        self._update_topics()

    def _on_down(self):
        self.model.daemon_lost()
        self.outer.set_visible_child_name("down")
        self.refresh()

    def _on_event(self, topic, data):
        self.model.apply_event(topic, data)
        if topic == "state":
            self.refresh()
            self.settings_page.sync_sensitivity(self.model.state["pointer"]["sensitivity"])
            self.profiles_page.refresh()
            self._update_topics()
        elif topic == "buttons":
            self.remote_page.refresh()
        elif topic == "motion":
            self.remote_page.refresh_gauges()
        elif topic == "ext":
            self.remote_page.refresh_extensions()

    def refresh(self):
        self.remote_page.refresh()

    def _update_topics(self):
        """Ask for the expensive streams only while the page showing them is visible."""
        topics = {"state", "buttons"}
        if self.remote_page.get_mapped() and self.model.connected:
            topics.add("motion")
            if self.model.extensions:
                topics.add("ext")
        self.link.set_topics(topics)

    def _page_mapped(self, page):
        if page is self.diagnostics_page:
            self.diagnostics_page.shown()
        self._update_topics()

    def _page_changed(self, stack, _param):
        if stack.get_visible_child_name() == "settings" and self.link.connected:
            self.command("config.get", lambda ok, r: ok and self.settings_page.load(r["options"]))
        if stack.get_visible_child_name() == "profiles" and self.link.connected:
            self.profiles_page.refresh()
        self._update_topics()

    def _start_service(self, _button):
        try:
            Gio.Subprocess.new(["systemctl", "--user", "start", "wiimote-mouse.service"], Gio.SubprocessFlags.NONE)
        except GLib.Error as exc:
            self.toast(f"Could not start the service: {exc.message}")

    def _on_close(self, _window):
        self.link.stop()
        return False


class Application(Adw.Application):
    def __init__(self, socket_path=None, flags=Gio.ApplicationFlags.DEFAULT_FLAGS):
        super().__init__(application_id=APP_ID, flags=flags)
        self.socket_path = socket_path
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._about)
        self.add_action(about)

    def do_activate(self):
        window = self.props.active_window or Window(self, self.socket_path)
        window.present()

    def _about(self, _action, _param):
        dialog = Adw.AboutDialog.new()
        dialog.set_application_name("Wii Remote Mouse")
        dialog.set_version(__version__)
        dialog.set_comments("Use a Wii Remote with MotionPlus as a gyro mouse.")
        dialog.set_application_icon("input-gaming")
        dialog.present(self.props.active_window)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wiimote-mouse-ui", description="Graphical front end for wiimote-mouse.")
    parser.add_argument("--socket", help="control socket path (default: the daemon's usual location)")
    parser.add_argument("--version", action="version", version=f"wiimote-mouse-ui {__version__}")
    args, rest = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return Application(args.socket).run([sys.argv[0], *rest])


if __name__ == "__main__":
    sys.exit(main())
