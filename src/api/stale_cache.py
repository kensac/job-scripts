"""A value that is slow to compute and slow to change, served stale.

The first read in a process waits for the computation. After that, a read
past the window returns the copy it has and starts one background thread to
recompute it, so no admin page waits on a lifetime scan once warm.
ponytail: per-process, so each api replica computes once per window; a
shared cache if replicas multiply.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# Every cache, so a test suite can start each test from cold.
_ALL: list[Stale] = []


def clear_all() -> None:
    for cache in _ALL:
        cache.clear()


class Stale[T]:
    def __init__(self, compute: Callable[[], T], ttl: Callable[[], float]) -> None:
        self._compute = compute
        self._ttl = ttl
        self._value: tuple[float, T] | None = None
        self._refreshing = threading.Lock()
        _ALL.append(self)

    def _refresh(self) -> None:
        """Recompute into the cache; the caller holds _refreshing."""
        try:
            self._value = (time.monotonic(), self._compute())
        finally:
            self._refreshing.release()

    def get(self) -> T:
        cached = self._value
        if cached is None:
            with self._refreshing:
                if self._value is None:
                    self._value = (time.monotonic(), self._compute())
            assert self._value is not None
            return self._value[1]
        if time.monotonic() - cached[0] >= self._ttl() and self._refreshing.acquire(blocking=False):
            threading.Thread(target=self._refresh, daemon=True).start()
        return cached[1]

    def clear(self) -> None:
        self._value = None
