"""How much work runs at once on this host.

Per host, so environment rather than app_config: a 1 GB host and a desktop
set different values. Nothing here touches the database.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from api import metrics

MAX_CONCURRENCY = int(os.environ.get("JOBTRACKER_MAX_CONCURRENCY", "6"))


class AdaptiveLimiter:
    """AIMD concurrency control on a rolling throughput window: grow while the
    completion rate keeps improving, step down when it stalls or errors appear,
    halve on rate limits. Each host converges to its own ceiling."""

    def __init__(
        self,
        min_c: int = 1,
        max_c: int = MAX_CONCURRENCY,
        window: int = 8,
        gauge: Any = metrics.WORKER_CONCURRENCY,
    ):
        self.limit = min(3, max_c)
        self.gauge = gauge
        self.min_c = min_c
        self.max_c = max_c
        self.window = window
        self._count = 0
        self._errors = 0
        self._win_start = time.monotonic()
        self._prev_rate: float | None = None

    def record(self, error: bool = False, rate_limited: bool = False) -> None:
        if rate_limited:
            self.limit = max(self.min_c, self.limit // 2)
            self._reset()
            return
        if error:
            self._errors += 1
        self._count += 1
        if self._count < self.window:
            return
        elapsed = time.monotonic() - self._win_start
        rate = self._count / elapsed if elapsed > 0 else 0.0
        if self._errors:
            self.limit = max(self.min_c, self.limit - 1)
        elif self._prev_rate is None or rate >= self._prev_rate * 1.05:
            self.limit = min(self.max_c, self.limit + 1)
        elif rate < self._prev_rate * 0.9:
            self.limit = max(self.min_c, self.limit - 1)
        self._prev_rate = rate
        self._reset()

    def _reset(self) -> None:
        self._count = 0
        self._errors = 0
        self._win_start = time.monotonic()
        if self.gauge is not None:
            self.gauge.set(self.limit)


def available_memory_mb(
    cgroup: Path = Path("/sys/fs/cgroup"), proc: Path = Path("/proc")
) -> float | None:
    """Memory this process could still take, in MB: the smaller of the
    container's cgroup v2 headroom and the host's MemAvailable. A container
    with no limit is bounded by the host alone, and a limited one on a full
    host by the host. Page cache the cgroup can reclaim (inactive_file) counts
    as free, as MemAvailable counts it. None where neither is readable (a
    laptop outside Linux): no reading, so nothing to back off on.

    ponytail: cgroup v2 only; a v1 host reads MemAvailable alone."""
    readings: list[float] = []
    try:
        for line in (proc / "meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                readings.append(int(line.split()[1]) / 1024)
    except (OSError, ValueError):
        pass
    try:
        limit = (cgroup / "memory.max").read_text().strip()
        if limit != "max":
            used = int((cgroup / "memory.current").read_text())
            stat = dict(line.split() for line in (cgroup / "memory.stat").read_text().splitlines())
            reclaimable = int(stat.get("inactive_file", 0))
            readings.append((int(limit) - used + reclaimable) / 2**20)
    except (OSError, ValueError):
        pass
    return min(readings) if readings else None


# In-flight jobs per worker inside a chunk (network time dominates, so calls
# overlap); the adaptive limiter tunes the actual level per host.


SCRAPE_CONCURRENCY = int(os.environ.get("JOBTRACKER_SCRAPE_CONCURRENCY", "2"))
