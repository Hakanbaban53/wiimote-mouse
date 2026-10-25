"""Process plumbing: signals, wakeups from worker threads, one-shot timers."""

import heapq
import itertools
import logging
import json
import os
import select
import signal
import time
from pathlib import Path




log = logging.getLogger("wiimote-mouse")



class Shutdown:
    """Signal handling that can also wake a blocking select()."""

    def __init__(self):
        self.requested = False
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)
        os.set_blocking(self.w, False)

    def install(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        signal.set_wakeup_fd(self.w, warn_on_full_buffer=False)

    def _on_signal(self, signum, frame):
        if self.requested:
            raise KeyboardInterrupt     # second signal: stop waiting for cleanup
        self.requested = True

    def request(self):
        """Ask for shutdown from anywhere (also used by tests)."""
        self.requested = True
        try:
            os.write(self.w, b"x")
        except OSError:
            pass

    def drain(self):
        try:
            os.read(self.r, 64)
        except BlockingIOError:
            pass

    def sleep(self, seconds):
        """Interruptible sleep; returns True if shutdown was requested."""
        if not self.requested:
            select.select([self.r], [], [], seconds)
            self.drain()
        return self.requested


class Wakeup:
    """Self-pipe so worker threads can wake the main select().

    Lives for the whole process on purpose: a late write from a worker must never
    hit a closed (and possibly reused) file descriptor.
    """

    def __init__(self):
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)
        os.set_blocking(self.w, False)

    def notify(self):
        try:
            os.write(self.w, b"x")
        except OSError:
            pass

    def drain(self):
        try:
            os.read(self.r, 256)
        except BlockingIOError:
            pass


class Timers:
    """Tiny one-shot scheduler driven by the select() timeout."""

    def __init__(self):
        self._heap = []
        self._seq = itertools.count()

    def after(self, delay, callback):
        heapq.heappush(self._heap, (time.monotonic() + delay, next(self._seq), callback))

    def next_deadline(self):
        return self._heap[0][0] if self._heap else None

    def run_due(self, now):
        while self._heap and self._heap[0][0] <= now:
            _, _, callback = heapq.heappop(self._heap)
            try:
                callback()
            except Exception:       # a broken callback must never kill the loop
                log.exception("Scheduled callback failed")


EXIT_OK, EXIT_FAILURE, EXIT_SETUP = 0, 1, 2


class StateStore:
    """Small things worth remembering between runs that aren't settings: the active profile."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self._dirty = False

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data = data if isinstance(data, dict) else {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        if self.data.get(key) != value:
            self.data[key] = value
            self._dirty = True

    def flush(self):
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as exc:
            log.debug("Could not save state: %s", exc)
