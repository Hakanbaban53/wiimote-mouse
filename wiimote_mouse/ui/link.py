"""A non-blocking connection to the daemon for GLib-based front ends.

Everything runs on the GLib main loop, so callbacks may touch widgets directly. If the daemon
is not running, or is restarted, the link keeps retrying and tells the UI when it is back.
"""

import json
import logging
import socket
from pathlib import Path

from gi.repository import GLib

from .. import control

log = logging.getLogger("wiimote-mouse")

RETRY_MS = 1000
LOST = "Connection to the daemon was lost"
NOT_RUNNING = "The daemon is not running"


class DaemonLink:
    def __init__(self, path=None, on_up=None, on_down=None, on_event=None, retry_ms=RETRY_MS):
        self.path = Path(path) if path else control.socket_path()
        self.on_up = on_up or (lambda state: None)
        self.on_down = on_down or (lambda: None)
        self.on_event = on_event or (lambda topic, data: None)
        self.retry_ms = retry_ms
        self.connected = False
        self._sock = None
        self._watch = None
        self._out_watch = None
        self._retry = None
        self._rbuf = b""
        self._wbuf = b""
        self._next_id = 1
        self._pending = {}              # request id -> callback(ok, result_or_error)
        self._desired = {"state"}       # topics the UI wants right now (state is always wanted)
        self._active = set()            # topics the daemon is actually sending us
        self._running = False

    # ---- public API
    def start(self):
        self._running = True
        self._try_connect()

    def stop(self):
        self._running = False
        self._remove(self._retry)
        self._retry = None
        self._disconnect(notify=False)

    def call(self, cmd, callback=None, **args):
        """Send a command. ``callback(ok, result)`` gets the result, or the error text."""
        if not self.connected:
            if callback:
                GLib.idle_add(callback, False, NOT_RUNNING)
            return
        request_id = self._next_id
        self._next_id += 1
        if callback:
            self._pending[request_id] = callback
        self._send({"id": request_id, "cmd": cmd, **args})

    def set_topics(self, topics):
        """Choose which event streams to receive. "state" is always included."""
        self._desired = set(topics) | {"state"}
        self._sync_topics()

    # ---- connecting
    def _try_connect(self):
        self._retry = None
        if not self._running:
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self.path))
        except OSError:
            sock.close()
            self._schedule_retry()
            return False
        sock.setblocking(False)
        self._sock = sock
        self._rbuf = self._wbuf = b""
        self.connected = True
        self._watch = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, sock.fileno(),
                                            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                                            self._on_readable)
        self._active = set()
        self._send({"id": 0, "cmd": "subscribe", "topics": sorted(self._desired)})
        self._pending[0] = self._on_subscribed
        self._active = set(self._desired)
        return False

    def _on_subscribed(self, ok, result):
        if ok:
            self.on_up(result["state"])
            self._sync_topics()
        else:
            log.warning("The daemon refused the subscription: %s", result)

    def _schedule_retry(self):
        if self._running and self._retry is None:
            self._retry = GLib.timeout_add(self.retry_ms, self._try_connect)

    @staticmethod
    def _remove(source):
        """Remove a GLib source unless it is already gone (GLib warns about a second removal)."""
        if source is not None and GLib.MainContext.default().find_source_by_id(source) is not None:
            GLib.source_remove(source)

    def _disconnect(self, notify=True):
        was_connected = self.connected
        self.connected = False
        for source in (self._watch, self._out_watch):
            self._remove(source)
        self._watch = self._out_watch = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        pending, self._pending = self._pending, {}
        for callback in pending.values():
            callback(False, LOST)
        self._active = set()
        if notify and was_connected:
            self.on_down()
        if notify:
            self._schedule_retry()

    # ---- topics
    def _sync_topics(self):
        if not self.connected or 0 in self._pending:
            return                      # the initial subscription is still in flight
        added = self._desired - self._active
        removed = self._active - self._desired
        if added:
            self.call("subscribe", topics=sorted(added))
        if removed:
            self.call("unsubscribe", topics=sorted(removed))
        self._active = set(self._desired)

    # ---- socket I/O
    def _send(self, message):
        self._wbuf += json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        self._flush()

    def _flush(self):
        while self._wbuf and self._sock is not None:
            try:
                sent = self._sock.send(self._wbuf)
            except (BlockingIOError, InterruptedError):
                if self._out_watch is None:
                    self._out_watch = GLib.unix_fd_add_full(
                        GLib.PRIORITY_DEFAULT, self._sock.fileno(), GLib.IOCondition.OUT, self._on_writable)
                return
            except OSError:
                self._disconnect()
                return
            self._wbuf = self._wbuf[sent:]

    def _on_writable(self, fd, condition):
        self._out_watch = None
        self._flush()
        return False

    def _on_readable(self, fd, condition):
        if self._sock is None:
            self._watch = None
            return False
        while self._sock is not None:
            try:
                data = self._sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                data = b""
            if not data:
                self._watch = None          # returning False below removes this source
                self._disconnect()
                return False
            self._rbuf += data
            self._dispatch_lines()
        return True

    def _dispatch_lines(self):
        while b"\n" in self._rbuf:
            line, self._rbuf = self._rbuf.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "event" in message:
                self.on_event(message["event"], message["data"])
                continue
            callback = self._pending.pop(message.get("id"), None)
            if callback is not None:
                ok = bool(message.get("ok"))
                callback(ok, message.get("result") if ok else message.get("error", "unknown error"))
