"""The UI's daemon link, driven by a plain GLib main context (no display needed)."""

import time
import unittest

try:
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
except (ImportError, ValueError):
    raise unittest.SkipTest("PyGObject is not installed")

from wiimote_mouse.control import ControlServer  # noqa: E402
from wiimote_mouse.ui.link import DaemonLink  # noqa: E402
from tests.helpers import temp_dir  # noqa: E402
from tests.test_control import Harness  # noqa: E402


def pump(predicate, timeout=3.0):
    """Run the default GLib context until predicate() is true (True) or time runs out (False)."""
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while context.iteration(False):
            pass
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.up, self.down, self.events = [], [], []
        self.links = []
        self.addCleanup(lambda: [link.stop() for link in self.links])

    def link(self, path, **kwargs):
        link = DaemonLink(path, on_up=self.up.append, on_down=lambda: self.down.append(1),
                          on_event=lambda t, d: self.events.append((t, d)), retry_ms=30, **kwargs)
        self.links.append(link)
        return link

    def handler(self):
        def handle(client, cmd, args):
            if cmd == "echo":
                return args
            raise Exception("boom") if cmd == "crash" else __import__("wiimote_mouse.control", fromlist=["x"]).ApiError("nope")
        return handle

    def test_connects_gets_the_state_and_answers_calls(self):
        h = Harness(self, handler=self.handler(), snapshot=lambda: {"connected": False})
        link = self.link(h.path)
        link.start()
        self.assertTrue(pump(lambda: self.up))
        self.assertEqual(self.up[0], {"connected": False})
        answers = []
        link.call("echo", lambda ok, result: answers.append((ok, result)), x=1)
        link.call("nope", lambda ok, result: answers.append((ok, result)))
        self.assertTrue(pump(lambda: len(answers) == 2))
        self.assertEqual(answers, [(True, {"x": 1}), (False, "nope")])

    def test_events_arrive_for_the_chosen_topics_only(self):
        h = Harness(self, handler=self.handler())
        link = self.link(h.path)
        link.start()
        self.assertTrue(pump(lambda: self.up))
        link.set_topics(["buttons"])
        self.assertTrue(pump(lambda: h.server.has_subscribers("buttons")))
        h.broadcast("buttons", {"button": "a", "pressed": True})
        h.broadcast("motion", {"yaw": 1})
        self.assertTrue(pump(lambda: self.events))
        self.assertEqual(self.events, [("buttons", {"button": "a", "pressed": True})])
        link.set_topics([])                                    # "state" stays
        self.assertTrue(pump(lambda: not h.server.has_subscribers("buttons")))
        self.assertTrue(h.server.has_subscribers("state"))

    def test_topics_chosen_before_connecting_are_applied_on_connect(self):
        h = Harness(self, handler=self.handler())
        link = self.link(h.path)
        link.set_topics(["motion", "buttons"])
        link.start()
        self.assertTrue(pump(lambda: h.server.has_subscribers("motion") and h.server.has_subscribers("buttons")))

    def test_waits_for_a_daemon_that_is_not_there_yet_and_survives_restarts(self):
        directory = temp_dir(self)
        path = f"{directory}/run/control.sock"
        link = self.link(path)
        link.start()
        pump(lambda: False, timeout=0.15)
        self.assertEqual((self.up, self.down), ([], []))
        first = ControlServer(path, self.handler(), lambda: {"n": 1})
        first.start()
        import select
        import threading
        stop = threading.Event()

        def serve(server):
            while not stop.is_set():
                r, w, _ = select.select(server.read_fds(), server.write_fds(), [], 0.02)
                server.service(r, w)

        thread = threading.Thread(target=serve, args=(first,), daemon=True)
        thread.start()
        self.assertTrue(pump(lambda: self.up))
        stop.set()
        thread.join(2)
        first.close()                                          # the daemon goes away
        self.assertTrue(pump(lambda: self.down))
        self.assertFalse(link.connected)
        answers = []
        link.call("echo", lambda ok, result: answers.append((ok, result)))
        self.assertTrue(pump(lambda: answers))
        self.assertEqual(answers[0][0], False)
        second = ControlServer(path, self.handler(), lambda: {"n": 2})
        second.start()
        stop2 = threading.Event()

        def serve2():
            while not stop2.is_set():
                r, w, _ = select.select(second.read_fds(), second.write_fds(), [], 0.02)
                second.service(r, w)

        thread2 = threading.Thread(target=serve2, daemon=True)
        thread2.start()
        self.addCleanup(lambda: (stop2.set(), thread2.join(2), second.close()))
        self.assertTrue(pump(lambda: len(self.up) == 2))
        self.assertEqual(self.up[1], {"n": 2})

    def test_pending_calls_fail_when_the_connection_drops(self):
        h = Harness(self, handler=self.handler())
        link = self.link(h.path)
        link.start()
        self.assertTrue(pump(lambda: self.up))
        results = []
        h.close()
        link.call("echo", lambda ok, result: results.append(ok))
        self.assertTrue(pump(lambda: results, timeout=3))
        self.assertEqual(results, [False])

    def test_stop_is_clean_and_idempotent(self):
        h = Harness(self, handler=self.handler())
        link = self.link(h.path)
        link.start()
        self.assertTrue(pump(lambda: self.up))
        link.stop()
        link.stop()
        self.assertFalse(link.connected)
        self.assertEqual(self.down, [])                        # a deliberate stop is not an outage


if __name__ == "__main__":
    unittest.main()
