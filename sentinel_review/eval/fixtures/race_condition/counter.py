# Fixture: race_condition
# Bug: shared counter incremented without a lock — data race in multi-threaded context

import threading


class RequestCounter:
    """Thread-safe request counter... except it isn't."""

    def __init__(self):
        # BUG: no lock protecting _count
        self._count = 0

    def increment(self):
        # BUG: read-modify-write is not atomic in Python (GIL helps but
        # doesn't guarantee correctness for compound operations on shared state)
        current = self._count
        # Simulated delay between read and write (realistic in async/I-O contexts)
        self._count = current + 1

    def get_count(self) -> int:
        return self._count


shared_counter = RequestCounter()


def handle_request(request_id: int) -> None:
    """Handle a request — increment global counter."""
    # BUG: shared_counter.increment() is not thread-safe
    shared_counter.increment()
