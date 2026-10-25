import json
import os
import queue
import select
import socket
import stat
import threading
import unittest
from pathlib import Path

from wiimote_mouse import control
from wiimote_mouse.client import Client, ClientError, ConnectionFailed
from wiimote_mouse.control import ApiError, ControlServer
from tests.helpers import temp_dir, wait_for


class Harness:
    """A ControlServer driven by its own thread, like the daemon's event loop would."""

    def __init__(self, test, handler=None, snapshot=None):
        self.path = Path(temp_dir(test)) / "run" / "control.sock"
        self.topic_changes = 0
        self.calls = []

        def default_handler(client, cmd, args):
            self.calls.append((cmd, args))
            if cmd == "echo":
                return args
            if cmd == "fail":
                raise ApiError("nope")
            if cmd == "crash":
                raise RuntimeError("boom")
            raise ApiError(f"unknown command '{cmd}'")

        def topics_changed():
            self.topic_changes += 1

        self.server = ControlServer(self.path, handler or default_handler,
                                    snapshot or (lambda: {"snap": True}), topics_changed)
        self.server.start()
        self._tasks = queue.Queue()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        test.addCleanup(self.close)

    def _loop(self):
        while not self._stop.is_set():
            readable, writable, _ = select.select(self.server.read_fds(), self.server.write_fds(), [], 0.02)
            self.server.service(readable, writable)
            while not self._tasks.empty():
                task, done = self._tasks.get()
                task()
                done.set()

    def run(self, fn):
        """Run fn on the server thread (the server isn't thread-safe, like the daemon)."""
        done = threading.Event()
        self._tasks.put((fn, done))
        done.wait(2)

    def broadcast(self, topic, data):
        self.run(lambda: self.server.broadcast(topic, data))

    def close(self):
        self._stop.set()
        self.thread.join(2)
        self.server.close()

    def raw(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(str(self.path))
        return sock


def read_line(sock):
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf)


class ProtocolTests(unittest.TestCase):
    def test_request_reply_and_ids(self):
        h = Harness(self)
        with Client(h.path) as client:
            self.assertEqual(client.call("echo", a=1, b=[2]), {"a": 1, "b": [2]})
            self.assertEqual(client.call("echo"), {})

    def test_errors_reach_the_client(self):
        h = Harness(self)
        with Client(h.path) as client:
            with self.assertRaisesRegex(ClientError, "nope"):
                client.call("fail")
            with self.assertRaisesRegex(ClientError, "unknown command 'zzz'"):
                client.call("zzz")
            with self.assertRaisesRegex(ClientError, "internal error"):
                client.call("crash")
            self.assertEqual(client.call("echo", ok=True), {"ok": True})       # still alive

    def test_malformed_requests_get_errors_not_disconnects(self):
        h = Harness(self)
        sock = h.raw()
        self.addCleanup(sock.close)
        for payload in (b"not json\n", b"[1,2]\n", b'{"cmd": 5}\n', b'{"id": 9}\n'):
            sock.sendall(payload)
            reply = read_line(sock)
            self.assertFalse(reply["ok"], payload)
        sock.sendall(b'{"id": 7, "cmd": "echo", "x": 1}\n')
        self.assertEqual(read_line(sock), {"id": 7, "ok": True, "result": {"x": 1}})

    def test_several_requests_in_one_packet(self):
        h = Harness(self)
        sock = h.raw()
        self.addCleanup(sock.close)
        sock.sendall(b'{"id":1,"cmd":"echo","n":1}\n{"id":2,"cmd":"echo","n":2}\n')
        buf = b""
        while buf.count(b"\n") < 2:
            buf += sock.recv(4096)
        self.assertEqual([json.loads(l)["id"] for l in buf.splitlines()], [1, 2])

    def test_oversized_request_closes_the_client(self):
        h = Harness(self)
        sock = h.raw()
        self.addCleanup(sock.close)
        sock.sendall(b"x" * (control.MAX_LINE + 10))
        self.assertTrue(wait_for(lambda: not h.server.clients))
        self.assertEqual(sock.recv(10), b"")


class SubscriptionTests(unittest.TestCase):
    def test_subscribe_returns_state_and_filters_events_by_topic(self):
        h = Harness(self, snapshot=lambda: {"mode": "ready"})
        with Client(h.path) as watcher, Client(h.path) as quiet:
            result = watcher.subscribe(["state", "buttons"])
            self.assertEqual(result, {"topics": ["buttons", "state"], "state": {"mode": "ready"}})
            quiet.subscribe(["motion"])
            h.broadcast("buttons", {"button": "a", "pressed": True})
            h.broadcast("motion", {"yaw": 1})
            h.broadcast("ext", {"nobody": "listens"})
            self.assertEqual(watcher.next_event(timeout=2), ("buttons", {"button": "a", "pressed": True}))
            self.assertEqual(quiet.next_event(timeout=2), ("motion", {"yaw": 1}))
            self.assertIsNone(watcher.next_event(timeout=0.1))

    def test_events_arriving_during_a_call_are_not_lost(self):
        h = Harness(self)
        with Client(h.path) as client:
            client.subscribe(["buttons"])
            h.broadcast("buttons", {"button": "b", "pressed": False})
            self.assertEqual(client.call("echo", x=1), {"x": 1})
            self.assertEqual(client.next_event(timeout=1), ("buttons", {"button": "b", "pressed": False}))

    def test_unsubscribe_and_topic_change_notifications(self):
        h = Harness(self)
        with Client(h.path) as client:
            client.subscribe(["ext"])
            self.assertEqual(h.topic_changes, 1)
            client.subscribe(["ext"])                               # no change
            self.assertEqual(h.topic_changes, 1)
            self.assertTrue(h.server.has_subscribers("ext"))
            self.assertEqual(client.call("unsubscribe", topics=["ext"])["topics"], [])
            self.assertEqual(h.topic_changes, 2)
            self.assertFalse(h.server.has_subscribers("ext"))

    def test_disconnecting_a_subscriber_counts_as_a_topic_change(self):
        h = Harness(self)
        client = Client(h.path).connect()
        client.subscribe(["motion"])
        client.close()
        self.assertTrue(wait_for(lambda: h.topic_changes == 2))
        self.assertEqual(h.server.topics_wanted(), set())

    def test_unknown_topics_are_rejected(self):
        h = Harness(self)
        with Client(h.path) as client:
            with self.assertRaisesRegex(ClientError, "unknown topic 'bogus'"):
                client.subscribe(["state", "bogus"])
            with self.assertRaisesRegex(ClientError, "list of topic names"):
                client.call("subscribe", topics="state")

    def test_a_client_that_never_reads_is_dropped(self):
        h = Harness(self)
        sock = h.raw()
        self.addCleanup(sock.close)
        sock.sendall(b'{"cmd":"subscribe","topics":["motion"]}\n')
        self.assertTrue(wait_for(lambda: h.server.has_subscribers("motion")))
        payload = {"blob": "x" * 100_000}
        for _ in range(60):
            h.broadcast("motion", payload)
        self.assertTrue(wait_for(lambda: not h.server.clients))
        with Client(h.path) as client:                              # the server is unharmed
            self.assertEqual(client.call("echo", ok=1), {"ok": 1})


class SocketSecurityTests(unittest.TestCase):
    def test_permissions(self):
        h = Harness(self)
        self.assertEqual(stat.S_IMODE(h.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(h.path.parent.stat().st_mode), 0o700)

    def test_loose_directory_permissions_are_tightened(self):
        directory = Path(temp_dir(self)) / "run"
        directory.mkdir(mode=0o755)
        os.chmod(directory, 0o755)
        server = ControlServer(directory / "control.sock", lambda *a: {})
        server.start()
        self.addCleanup(server.close)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_stale_socket_is_replaced_but_a_live_one_is_not(self):
        directory = Path(temp_dir(self)) / "run"
        path = directory / "control.sock"
        first = ControlServer(path, lambda *a: {})
        first.start()
        with self.assertRaises(control.AddressInUse):
            ControlServer(path, lambda *a: {}).start()
        first.listener.close()                                      # crash: the file stays behind
        first.listener = None
        self.assertTrue(path.exists())
        second = ControlServer(path, lambda *a: {})
        second.start()
        self.addCleanup(second.close)
        self.assertTrue(path.exists())

    def test_close_removes_the_socket(self):
        h = Harness(self)
        h.close()
        self.assertFalse(h.path.exists())

    def test_default_socket_path_follows_xdg_runtime_dir(self):
        old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/1234"
        try:
            self.assertEqual(control.socket_path(), Path("/run/user/1234/wiimote-mouse/control.sock"))
            del os.environ["XDG_RUNTIME_DIR"]
            self.assertIn(str(os.getuid()), str(control.socket_path()))
        finally:
            if old is not None:
                os.environ["XDG_RUNTIME_DIR"] = old


class ClientTests(unittest.TestCase):
    def test_no_daemon(self):
        path = Path(temp_dir(self)) / "missing.sock"
        with self.assertRaisesRegex(ConnectionFailed, "not running"):
            Client(path).connect()

    def test_a_daemon_that_does_not_answer(self):
        directory = Path(temp_dir(self))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(directory / "silent.sock"))
        listener.listen(1)
        self.addCleanup(listener.close)
        with Client(directory / "silent.sock", timeout=0.2) as client:
            with self.assertRaisesRegex(ConnectionFailed, "did not answer"):
                client.call("status")

    def test_a_daemon_that_goes_away(self):
        h = Harness(self)
        client = Client(h.path).connect()
        self.addCleanup(client.close)
        h.close()
        with self.assertRaises(ConnectionFailed):
            client.call("echo")


if __name__ == "__main__":
    unittest.main()
