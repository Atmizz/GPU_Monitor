"""Retry limits shared by node connections and individual GPU queries."""

import threading
import time


class RetryBackoff:
    def __init__(self, initial=30, maximum=300, clock=None):
        self.initial = initial
        self.maximum = maximum
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.entries = {}

    def pending(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return 0, ''
            remaining = max(0, entry['until'] - self.clock())
            return remaining, entry['error']

    def failure(self, key, error):
        with self.lock:
            count = self.entries.get(key, {}).get('count', 0) + 1
            delay = min(self.maximum, self.initial * 2 ** min(count - 1, 16))
            self.entries[key] = {'count': count, 'until': self.clock() + delay, 'error': error}
            return delay

    def success(self, key):
        with self.lock:
            self.entries.pop(key, None)
