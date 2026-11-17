"""Per-host politeness shared by every bot in a fleet."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, Optional


class BudgetExceeded(Exception):
    """The per-host request limit was reached."""


class _Host:
    __slots__ = ("lock", "next_at", "count")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.count = 0


class HostLimiter:
    """One request at a time per host, with a minimum gap between requests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._hosts: Dict[str, _Host] = {}
        self._meta = threading.Lock()

    def _host(self, host: str) -> _Host:
        with self._meta:
            return self._hosts.setdefault(host, _Host())

    @contextmanager
    def slot(self, host: str, delay: float, limit: Optional[int] = None, count: bool = True) -> Iterator[None]:
        state = self._host(host)
        with state.lock:
            if count and limit is not None and state.count >= limit:  # checked under the lock, so it cannot be raced
                raise BudgetExceeded(host)
            wait = state.next_at - self._clock()
            if wait > 0:
                self._sleep(wait)
            state.count += count
            try:
                yield
            finally:
                with self._meta:
                    state.next_at = max(state.next_at, self._clock() + delay)

    def penalize(self, host: str, seconds: float) -> None:
        """Push the host's next allowed request further out (429 / 503)."""
        state = self._host(host)
        with self._meta:
            state.next_at = max(state.next_at, self._clock() + seconds)

    def count(self, host: str) -> int:
        return self._host(host).count
