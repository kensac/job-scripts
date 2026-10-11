"""A worker running several tasks at once (api.worker._Slots).

What has to hold when tasks share a process: each runs exactly once, each
writes only under its own claim, SIGTERM releases every one of them, and the
limit backs off when memory runs short.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from api import db, worker
from tasks.runtime import lifecycle

KIND = "slots_probe"


@pytest.fixture
def slots(monkeypatch):
    """A worker with room for three, the probe kind threaded, and no memory
    reading unless a test supplies one."""
    monkeypatch.setattr(worker, "THREADED_KINDS", frozenset({KIND}))
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(worker, "available_memory_mb", lambda: None)
    s = worker._Slots()
    s.configure(3, {}, 512)
    s.limiter.limit = 3
    yield s
    deadline = time.monotonic() + 10
    while worker._in_flight and time.monotonic() < deadline:
        time.sleep(0.02)


def _drive(s: worker._Slots, until, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    with asyncio.Runner() as runner:
        while not until():
            assert time.monotonic() < deadline, "the worker did not get there"
            s.step(runner)


def _row(task_id: int):
    return db.query_one("SELECT status, attempts, worker FROM tasks WHERE id = %s", (task_id,))


def test_each_task_runs_once_and_several_run_at_once(monkeypatch, slots, f):
    ids = [f.make_task(KIND) for _ in range(7)]
    runs: list[int] = []
    claims: dict[int, int | None] = {}
    lock = threading.Lock()
    now = {"in": 0, "peak": 0}

    async def probe(task_id, payload):
        claim = lifecycle._current_claim.get()
        with lock:
            runs.append(task_id)
            claims[task_id] = claim.task_id if claim else None
            now["in"] += 1
            now["peak"] = max(now["peak"], now["in"])
        await asyncio.sleep(0.15)
        with lock:
            now["in"] -= 1

    monkeypatch.setitem(worker.HANDLERS, KIND, probe)
    _drive(
        slots,
        lambda: (
            db.query_one("SELECT COUNT(*) AS n FROM tasks WHERE status = 'done'")["n"] == len(ids)
            and not slots.held
        ),
    )
    assert sorted(runs) == sorted(ids), "a task ran twice or not at all"
    assert all(_row(t)["attempts"] == 1 for t in ids)
    # Each handler saw its own claim, never a sibling's.
    assert claims == {t: t for t in ids}
    assert 1 < now["peak"] <= 3


def test_a_lost_claim_stops_only_its_own_task(monkeypatch, slots, f):
    """Task A is reclaimed elsewhere mid-run; its finish must be refused while
    B, running beside it, finishes. A process-wide claim would guard both
    writes with whichever claim was set last."""
    a, b = f.make_task(KIND, {"lose": True}), f.make_task(KIND)
    both = threading.Barrier(2, timeout=5)

    async def probe(task_id, payload):
        await asyncio.to_thread(both.wait)
        if payload.get("lose"):
            db.execute(
                "UPDATE tasks SET attempts = attempts + 1, worker = 'other-host' WHERE id = %s",
                (task_id,),
            )

    monkeypatch.setitem(worker.HANDLERS, KIND, probe)
    _drive(slots, lambda: _row(b)["status"] == "done" and not slots.held)
    assert _row(a) == {"status": "running", "attempts": 2, "worker": "other-host"}


def test_sigterm_releases_every_task_it_holds(monkeypatch, slots, f):
    held = [f.make_task(KIND) for _ in range(2)]
    gone = f.make_task(KIND, {"lose": True})
    release = threading.Event()
    started = threading.Barrier(4, timeout=5)

    async def probe(task_id, payload):
        if payload.get("lose"):
            db.execute(
                "UPDATE tasks SET attempts = attempts + 1, worker = 'other-host' WHERE id = %s",
                (task_id,),
            )
        await asyncio.to_thread(started.wait)
        await asyncio.to_thread(release.wait, 5)

    monkeypatch.setitem(worker.HANDLERS, KIND, probe)
    with asyncio.Runner() as runner:
        for _ in range(3):
            slots.step(runner)
    started.wait()
    assert sorted(worker._in_flight) == sorted([*held, gone])

    def no_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", no_exit)
    with pytest.raises(SystemExit):
        worker._graceful_exit(15, None)
    for t in held:
        assert _row(t) == {"status": "pending", "attempts": 0, "worker": worker.WORKER_NAME}
    # Taken by another worker before the signal: not ours to hand back.
    assert _row(gone)["status"] == "running" and _row(gone)["worker"] == "other-host"
    # The handlers ending after the release cannot finish what was handed back.
    # Waited on directly: a step would claim the released tasks again.
    release.set()
    deadline = time.monotonic() + 10
    while worker._in_flight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not worker._in_flight
    assert all(_row(t)["status"] == "pending" for t in held)


def test_memory_pressure_halves_the_limit_and_stops_claims(monkeypatch, slots, f):
    running = [f.make_task(KIND) for _ in range(2)]
    waiting = f.make_task(KIND)
    release = threading.Event()

    async def probe(task_id, payload):
        await asyncio.to_thread(release.wait, 5)

    monkeypatch.setitem(worker.HANDLERS, KIND, probe)
    slots.limiter.limit = 4
    with asyncio.Runner() as runner:
        slots.step(runner)
        slots.step(runner)
        assert sorted(slots.held) == running
        monkeypatch.setattr(worker, "available_memory_mb", lambda: 100.0)
        slots.step(runner)
        assert slots.limiter.limit == 2
        assert _row(waiting)["status"] == "pending", "claimed with no memory to run it"
        slots.step(runner)
        assert slots.limiter.limit == 1
        # Memory back: still nothing until the held tasks end, because the
        # limit is now below what is held.
        monkeypatch.setattr(worker, "available_memory_mb", lambda: 4096.0)
        slots.step(runner)
        assert _row(waiting)["status"] == "pending"
    release.set()
    _drive(slots, lambda: _row(waiting)["status"] == "done")


def test_a_task_failing_on_exhaustion_halves_the_limit(monkeypatch, slots, f):
    f.make_task(KIND)

    async def probe(task_id, payload):
        raise OSError("[Errno 12] Cannot allocate memory")

    monkeypatch.setitem(worker.HANDLERS, KIND, probe)
    slots.limiter.limit = 3
    _drive(slots, lambda: slots.limiter.limit == 1)


def test_available_memory_is_the_tighter_of_cgroup_and_host(tmp_path: Path):
    from tasks.runtime.limits import available_memory_mb

    proc, cg = tmp_path / "proc", tmp_path / "cg"
    proc.mkdir()
    cg.mkdir()
    (proc / "meminfo").write_text("MemTotal: 4000000 kB\nMemAvailable: 2048000 kB\n")
    assert available_memory_mb(cg, proc) == 2000
    (cg / "memory.max").write_text(f"{1024 * 2**20}\n")
    (cg / "memory.current").write_text(f"{900 * 2**20}\n")
    (cg / "memory.stat").write_text(f"anon 1\ninactive_file {100 * 2**20}\n")
    assert available_memory_mb(cg, proc) == 224
    (cg / "memory.max").write_text("max\n")
    assert available_memory_mb(cg, proc) == 2000
