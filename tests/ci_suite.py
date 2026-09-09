from __future__ import annotations

import hashlib
import json
import os
import platform
import time
from pathlib import Path

import pytest

_STARTED = pytest.StashKey[float]()


def pytest_addoption(parser):
    group = parser.getgroup("test execution")
    group.addoption("--shard-count", type=int, default=1)
    group.addoption("--shard-index", type=int, default=0)
    group.addoption("--test-report", help="write selected cases and phase timings as JSON")


def pytest_load_initial_conftests(early_config):
    early_config.stash[_STARTED] = time.perf_counter()


def pytest_configure(config):
    count, index = config.getoption("shard_count"), config.getoption("shard_index")
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("--shard-index must be between 0 and --shard-count minus one")
    output = config.getoption("test_report")
    if output:
        config.pluginmanager.register(_Report(config, Path(output).resolve()), "test-phase-report")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    count, index = config.getoption("shard_count"), config.getoption("shard_index")
    if count == 1:
        return
    selected, deselected = [], []
    for item in items:
        # Stable across processes and Python hash seeds. Partition after -m/-k
        # selection; each job must still hold its own isolated database.
        slot = int.from_bytes(hashlib.sha256(item.nodeid.encode()).digest()[:8]) % count
        (selected if slot == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


class _Report:
    def __init__(self, config, output):
        self.config = config
        self.output = output
        self.started = config.stash.get(_STARTED, time.perf_counter())
        self.selected = []
        self.reports = {}

    def pytest_collection_finish(self, session):
        self.selected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        self.reports.setdefault(report.nodeid, []).append(
            {
                "phase": report.when,
                "seconds": report.duration,
                "outcome": report.outcome,
                "subtest": hasattr(report, "context"),
            }
        )

    def pytest_sessionfinish(self, session, exitstatus):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(
            json.dumps(
                {
                    "revision": os.environ.get("TEST_REVISION"),
                    "lane": os.environ.get("TEST_LANE", "serial"),
                    "repetition": int(os.environ.get("TEST_REPETITION", "1")),
                    "python": platform.python_version(),
                    "shard_count": self.config.getoption("shard_count"),
                    "shard_index": self.config.getoption("shard_index"),
                    "pytest_seconds": time.perf_counter() - self.started,
                    "exitstatus": int(exitstatus),
                    "selected": self.selected,
                    "reports": self.reports,
                },
                indent=2,
            )
            + "\n"
        )
