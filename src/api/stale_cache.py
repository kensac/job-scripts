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
_ALL: list[Stale | StaleByKey] = []


def clear_all() -> None:
    for cache in _ALL:
        cache.clear()


class Stale[T]:
    def __init__(
        self, compute: Callable[[], T], ttl: Callable[[], float], *, register: bool = True
    ) -> None:
        self._compute = compute
        self._ttl = ttl
        self._value: tuple[float, T] | None = None
        self._refreshing = threading.Lock()
        if register:
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

    def warm(self) -> None:
        """Start computing in the background, so the first reader after a
        start finds a value instead of waiting for one."""
        if self._refreshing.acquire(blocking=False):
            threading.Thread(target=self._refresh, daemon=True).start()

    def clear(self) -> None:
        self._value = None


class StaleByKey[K, T]:
    """One Stale per key, for a page whose answer depends on its query.
    ponytail: forgets every key past 256 rather than tracking use."""

    def __init__(self, ttl: Callable[[], float]) -> None:
        self._ttl = ttl
        self._caches: dict[K, Stale[T]] = {}
        self._lock = threading.Lock()
        _ALL.append(self)

    def _cache(self, key: K, compute: Callable[[], T]) -> Stale[T]:
        with self._lock:
            cache = self._caches.get(key)
            if cache is None:
                if len(self._caches) >= 256:
                    self._caches.clear()
                cache = self._caches[key] = Stale(compute, self._ttl, register=False)
            return cache

    def get(self, key: K, compute: Callable[[], T]) -> T:
        return self._cache(key, compute).get()

    def warm(self, key: K, compute: Callable[[], T]) -> None:
        self._cache(key, compute).warm()

    def clear(self) -> None:
        with self._lock:
            self._caches.clear()
