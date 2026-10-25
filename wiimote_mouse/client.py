"""Client for the daemon's control socket. Used by wiimotectl and the UI."""

import json
import socket
from collections import deque
from pathlib import Path

from . import control


class ClientError(Exception):
    """The daemon rejected a command (its message is the error text)."""


class ConnectionFailed(ClientError):
    """The daemon can't be reached."""


class Client:
    """Blocking client: ``call()`` sends a command and waits for its reply; events that
    arrive meanwhile are queued and returned by ``next_event()``."""

    def __init__(self, path=None, timeout=3.0):
        self.path = Path(path) if path else control.socket_path()
        self.timeout = timeout
        self.sock = None
        self._buf = b""
        self._next_id = 1
        self._events = deque()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.path))
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            raise ConnectionFailed(
                f"The wiimote-mouse daemon is not running (nothing listens on {self.path}). "
                "Start it with: systemctl --user start wiimote-mouse.service") from None
        except OSError as exc:
            sock.close()
            raise ConnectionFailed(f"Cannot connect to {self.path}: {exc}") from None
        self.sock = sock
        return self

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _read_message(self, timeout):
        """Next JSON line from the socket, or None if ``timeout`` elapsed first."""
        self.sock.settimeout(timeout)
        while b"\n" not in self._buf:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError as exc:
                raise ConnectionFailed(f"Connection lost: {exc}") from None
            if not data:
                raise ConnectionFailed("The daemon closed the connection")
            self._buf += data
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, cmd, **args):
        """Run one command and return its result. Raises ClientError on failure."""
        request_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"id": request_id, "cmd": cmd, **args}).encode("utf-8") + b"\n"
        try:
            self.sock.sendall(payload)
        except OSError as exc:
            raise ConnectionFailed(f"Connection lost: {exc}") from None
        while True:
            message = self._read_message(self.timeout)
            if message is None:
                raise ConnectionFailed(f"The daemon did not answer '{cmd}' in {self.timeout:g} s")
            if "event" in message:
                self._events.append((message["event"], message["data"]))
                continue
            if message.get("id") != request_id:
                continue
            if message.get("ok"):
                return message.get("result")
            raise ClientError(message.get("error", "unknown error"))

    def subscribe(self, topics):
        return self.call("subscribe", topics=list(topics))

    def next_event(self, timeout=None):
        """The next (topic, data) event, or None if none arrived within ``timeout``."""
        if self._events:
            return self._events.popleft()
        while True:
            message = self._read_message(timeout)
            if message is None:
                return None
            if "event" in message:
                return message["event"], message["data"]
