"""Local control socket: newline-delimited JSON over a Unix domain socket.

Protocol (version 1)::

    -> {"id": 1, "cmd": "status"}
    <- {"id": 1, "ok": true, "result": {...}}
    <- {"id": 2, "ok": false, "error": "unknown command 'x'"}

After ``{"cmd": "subscribe", "topics": ["state", "buttons"]}`` the server also pushes
events, which carry no id::

    <- {"event": "buttons", "data": {"button": "a", "pressed": true}}

The socket lives in ``$XDG_RUNTIME_DIR/wiimote-mouse/`` (directory mode 0700, socket
mode 0600), so only the owning user can talk to the daemon. Everything runs on the
daemon's single event loop: nothing here blocks and there are no threads.
"""

import json
import logging
import os
import socket
from pathlib import Path

log = logging.getLogger("wiimote-mouse")

PROTOCOL_VERSION = 1
TOPICS = ("state", "buttons", "motion", "ext")
MAX_LINE = 64 * 1024            # a request longer than this is a protocol error
MAX_BACKLOG = 512 * 1024        # bytes queued for a slow client before it is dropped


class ApiError(Exception):
    """A command failed in a way the client should see."""


class AddressInUse(Exception):
    """Another daemon is already listening on the control socket."""


def socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "wiimote-mouse" / "control.sock"
    return Path(f"/tmp/wiimote-mouse-{os.getuid()}") / "control.sock"


class Client:
    def __init__(self, sock):
        self.sock = sock
        self.rbuf = b""
        self.wbuf = bytearray()
        self.topics = set()

    def fileno(self):
        return self.sock.fileno()


class ControlServer:
    """Accepts clients, parses their requests and pushes events.

    ``handler(client, cmd, args)`` returns a JSON-able result or raises ApiError.
    ``snapshot()`` supplies the state attached to subscribe replies, and
    ``on_topics_changed()`` runs whenever the set of subscribed topics changes (so the
    daemon can start or stop expensive streams).
    """

    def __init__(self, path, handler, snapshot=None, on_topics_changed=None):
        self.path = Path(path)
        self.handler = handler
        self.snapshot = snapshot
        self.on_topics_changed = on_topics_changed
        self.listener = None
        self.clients = []

    # ---- lifecycle
    def start(self):
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid():
            raise PermissionError(f"{directory} is owned by another user")
        if info.st_mode & 0o077:
            os.chmod(directory, 0o700)
        if self.path.exists() or self.path.is_symlink():
            self._remove_stale()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(8)
            listener.setblocking(False)
        except OSError:
            listener.close()
            raise
        self.listener = listener

    def _remove_stale(self):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(str(self.path))
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
            self.path.unlink(missing_ok=True)       # nobody is listening: leftover from a crash
        except OSError:
            self.path.unlink(missing_ok=True)
        else:
            raise AddressInUse(f"another wiimote-mouse daemon is already running ({self.path})")
        finally:
            probe.close()

    def close(self):
        for client in list(self.clients):
            self._drop(client, notify=False)
        if self.listener is not None:
            self.listener.close()
            self.listener = None
            self.path.unlink(missing_ok=True)

    # ---- select integration
    def read_fds(self):
        return ([self.listener] if self.listener is not None else []) + self.clients

    def write_fds(self):
        return [client for client in self.clients if client.wbuf]

    def service(self, readable, writable=()):
        for item in readable:
            if item is self.listener:
                self._accept()
            elif item in self.clients:
                self._read(item)
        for client in writable:
            if client in self.clients:
                self._flush(client)

    # ---- topics
    def has_subscribers(self, topic):
        return any(topic in client.topics for client in self.clients)

    def topics_wanted(self):
        wanted = set()
        for client in self.clients:
            wanted |= client.topics
        return wanted

    def broadcast(self, topic, data):
        if not self.has_subscribers(topic):
            return
        line = self._encode({"event": topic, "data": data})
        for client in list(self.clients):
            if topic in client.topics:
                self._queue(client, line)

    # ---- internals
    def _accept(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            sock.setblocking(False)
            self.clients.append(Client(sock))

    def _read(self, client):
        try:
            data = client.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(client)
            return
        if not data:
            self._drop(client)
            return
        client.rbuf += data
        while b"\n" in client.rbuf:
            line, client.rbuf = client.rbuf.split(b"\n", 1)
            if line.strip():
                self._handle(client, line)
            if client not in self.clients:
                return
        if len(client.rbuf) > MAX_LINE:
            self._drop(client)

    def _handle(self, client, line):
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("cmd"), str):
                raise ApiError("a request must be an object with a 'cmd' string")
            request_id = request.get("id")
            cmd = request["cmd"]
            args = {k: v for k, v in request.items() if k not in ("id", "cmd")}
            if cmd in ("subscribe", "unsubscribe"):
                result = self._topics(client, cmd, args)
            else:
                result = self.handler(client, cmd, args)
            reply = {"ok": True, "result": result}
        except ApiError as exc:
            reply = {"ok": False, "error": str(exc)}
        except ValueError as exc:                    # includes invalid JSON
            reply = {"ok": False, "error": f"bad request: {exc}"}
        except Exception:
            log.exception("Control command failed")
            reply = {"ok": False, "error": "internal error"}
        if request_id is not None:
            reply["id"] = request_id
        if client in self.clients:
            self._queue(client, self._encode(reply))

    def _topics(self, client, cmd, args):
        topics = args.get("topics", list(TOPICS) if cmd == "subscribe" else [])
        if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
            raise ApiError("'topics' must be a list of topic names")
        unknown = [t for t in topics if t not in TOPICS]
        if unknown:
            raise ApiError(f"unknown topic '{unknown[0]}' (use {', '.join(TOPICS)})")
        before = set(client.topics)
        if cmd == "subscribe":
            client.topics |= set(topics)
        else:
            client.topics -= set(topics) if topics else set(client.topics)
        if client.topics != before and self.on_topics_changed is not None:
            self.on_topics_changed()
        result = {"topics": sorted(client.topics)}
        if cmd == "subscribe" and self.snapshot is not None:
            result["state"] = self.snapshot()
        return result

    @staticmethod
    def _encode(obj):
        return json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8") + b"\n"

    def _queue(self, client, line):
        client.wbuf += line
        if len(client.wbuf) > MAX_BACKLOG:
            log.debug("Dropping a control client that isn't reading")
            self._drop(client)
            return
        self._flush(client)

    def _flush(self, client):
        if not client.wbuf:
            return
        try:
            sent = client.sock.send(client.wbuf)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(client)
            return
        del client.wbuf[:sent]

    def _drop(self, client, notify=True):
        if client not in self.clients:
            return
        self.clients.remove(client)
        had_topics = bool(client.topics)
        try:
            client.sock.close()
        except OSError:
            pass
        if notify and had_topics and self.on_topics_changed is not None:
            self.on_topics_changed()
