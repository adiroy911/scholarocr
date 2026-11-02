"""Per-user sliding-window limits (in memory; one process). Use a shared store for several replicas."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable


class RateLimited(Exception):
    def __init__(self, what: str, retry_after_s: float):
        super().__init__(f"rate limit reached for {what}; retry in {int(retry_after_s) + 1} s")
        self.what = what
        self.retry_after_s = retry_after_s


class SlidingWindow:
    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self._events: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, q: deque, now: float) -> None:
        while q and now - q[0][0] >= self.window_s:
            q.popleft()

    def used(self, key: str) -> int:
        with self._lock:
            q = self._events[key]
            self._trim(q, self.clock())
            return sum(n for _, n in q)

    def check(self, key: str, what: str, cost: int = 1) -> None:
        """Raise RateLimited if adding ``cost`` would exceed the limit (does not record)."""
        if self.limit <= 0:
            return
        with self._lock:
            now = self.clock()
            q = self._events[key]
            self._trim(q, now)
            if sum(n for _, n in q) + cost > self.limit:
                retry = self.window_s - (now - q[0][0]) if q else self.window_s
                raise RateLimited(what, max(retry, 0.0))

    def record(self, key: str, cost: int = 1) -> None:
        if self.limit <= 0 or cost <= 0:
            return
        with self._lock:
            self._events[key].append((self.clock(), cost))

    def hit(self, key: str, what: str, cost: int = 1) -> None:
        self.check(key, what, cost)
        self.record(key, cost)


class UserLimits:
    def __init__(self, calls_per_minute: int, ocr_calls_per_hour: int, pages_per_day: int,
                 clock: Callable[[], float] = time.monotonic):
        self.calls = SlidingWindow(calls_per_minute, 60.0, clock)
        self.ocr_calls = SlidingWindow(ocr_calls_per_hour, 3600.0, clock)
        self.pages = SlidingWindow(pages_per_day, 86400.0, clock)

    def snapshot(self, user_id: str) -> dict:
        return {"tool_calls_last_minute": {"used": self.calls.used(user_id), "limit": self.calls.limit},
                "ocr_calls_last_hour": {"used": self.ocr_calls.used(user_id), "limit": self.ocr_calls.limit},
                "pages_last_24h": {"used": self.pages.used(user_id), "limit": self.pages.limit}}
