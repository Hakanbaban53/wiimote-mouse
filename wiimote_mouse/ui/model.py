"""What the UI knows about the daemon and the remote, built from the daemon's state and events.

Pure Python (no GTK), so it can be tested without a display.
"""

DISCONNECTED = {"connected": False, "remote": None, "pointer": {"sensitivity": None},
                "profile": {"active": None, "available": []}}


class RemoteModel:
    def __init__(self):
        self.daemon_up = False
        self.state = dict(DISCONNECTED)
        self.pressed = set()            # buttons currently held down
        self.motion = None              # the latest "motion" event
        self.ext = {}                   # extension device -> {code name: value}

    # ---- updates
    def daemon_lost(self):
        self.daemon_up = False
        self.state = dict(DISCONNECTED)
        self.pressed.clear()
        self.motion = None
        self.ext.clear()

    def daemon_found(self, state):
        self.daemon_up = True
        self.apply_state(state)

    def apply_state(self, state):
        self.state = state
        if not state.get("connected"):
            self.pressed.clear()
            self.motion = None
            self.ext.clear()
        else:
            present = set(state["remote"]["extensions"])
            for device in list(self.ext):
                if device not in present:
                    del self.ext[device]

    def apply_event(self, topic, data):
        if topic == "state":
            self.apply_state(data)
        elif topic == "buttons":
            (self.pressed.add if data["pressed"] else self.pressed.discard)(data["button"])
        elif topic == "motion":
            self.motion = data
        elif topic == "ext":
            self.ext.setdefault(data["device"], {}).update(data["values"])

    # ---- what the views need
    @property
    def connected(self):
        return bool(self.state.get("connected"))

    @property
    def remote(self):
        return self.state.get("remote")

    @property
    def mode(self):
        remote = self.remote
        return remote["mode"] if remote else None

    @property
    def extensions(self):
        remote = self.remote
        return list(remote["extensions"]) if remote else []

    def remote_view(self):
        """The inputs of the remote drawing."""
        remote = self.remote or {}
        return {"connected": self.connected, "asleep": bool(remote.get("asleep")),
                "pressed": set(self.pressed), "leds": remote.get("leds")}

    def title(self):
        if not self.daemon_up:
            return "The daemon is not running"
        if not self.connected:
            return "Waiting for a Wii Remote"
        return {"ready": "Ready", "calibrating": "Calibrating: hold the remote still",
                "waiting": "Waiting for the MotionPlus", "asleep": "Sensors asleep: press any button"
                }.get(self.mode, "Connected")
