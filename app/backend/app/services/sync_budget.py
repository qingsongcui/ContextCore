"""Cooperative background work budget; foreground reads do not inherit it."""
import threading
import time


class SyncCancelled(Exception):
    pass


class SyncBudget:
    # Limit independent sources per pass. Grouped adapters retain atomic reads.
    max_sources = 16

    def __init__(self, stop: threading.Event, duty_cycle: float = 0.2):
        self.stop = stop
        self.duty_cycle = duty_cycle
        self.started = time.monotonic()
        self.cpu_started = time.thread_time()

    def checkpoint(self):
        if self.stop.is_set():
            raise SyncCancelled()
        cpu = time.thread_time() - self.cpu_started
        delay = cpu / self.duty_cycle - (time.monotonic() - self.started)
        # No database transaction is held at a checkpoint. Cancellation wakes
        # this wait immediately; only committed sources receive cursor receipts.
        if delay > 0 and self.stop.wait(delay):
            raise SyncCancelled()
