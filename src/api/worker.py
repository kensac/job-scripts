"""Worker process lifecycle, concurrent slots, and task claim ownership.

Queue claims, cycle scheduling, and task execution live in focused modules.
Handlers live in src/tasks/.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import signal
import socket
import threading
import time
from queue import SimpleQueue
from typing import Any

from api import (
    data_level,
    db,
    hosts,
    metrics,
    telemetry,
)
from api.queue import enqueue as enqueue
from api.worker_lifecycle import claims as worker_claims
from api.worker_lifecycle import execution as worker_execution
from api.worker_lifecycle import scheduling as worker_scheduling
from api.worker_lifecycle.claims import EXCLUDE_KINDS, WORKER_KINDS
from api.worker_lifecycle.claims import _kinds_clause as _kinds_clause
from api.worker_lifecycle.execution import _is_transient as _is_transient
from core import pool
from tasks import HANDLERS as HANDLERS
from tasks.runtime import (
    AdaptiveLimiter,
    TaskClaim,
    available_memory_mb,
    reconcile_chunks,
    set_current_claim,
)
from tasks.runtime import (
    AwaitingBatch as AwaitingBatch,
)

logger = logging.getLogger(__name__)


POLL_SECONDS = float(os.environ.get("JOBTRACKER_WORKER_POLL", "5"))
_managed_board_schedule_attempts = worker_scheduling._managed_board_schedule_attempts
schedule_ingest_cycle = worker_scheduling.schedule_ingest_cycle


def _claim_task(too_heavy: list[str] | None = None) -> dict[str, Any] | None:
    return worker_claims.claim_task(WORKER_NAME, too_heavy)


def reap_stale_tasks() -> None:
    worker_claims.reap_stale_tasks(WORKER_NAME)


# Stamped on every claimed task so the admin UI can attribute work (and
# failures - e.g. one host's IP getting blocked) to a fleet host. Set it in
# compose; the container hostname fallback is a random hex id.
WORKER_NAME = os.environ.get("JOBTRACKER_WORKER_NAME") or socket.gethostname()

# This host's own task slots, set by the host's config beside its name. Unlike
# a worker_task_slots entry it is known before the database is read, so the
# limit starts there rather than at one: from one, every deploy restarted each
# worker at one task and it climbed back a window of completions at a time.
# Unset, the worker reads worker_task_slots alone, as it did before this.
_TASK_SLOTS_ENV = os.environ.get("JOBTRACKER_TASK_SLOTS")
TASK_SLOTS = int(_TASK_SLOTS_ENV) if _TASK_SLOTS_ENV else None

# Every claim this process holds, by task id. A handler reads its own claim
# from the runtime contextvar, which is per task: each task runs in its own
# asyncio task, and a threaded one on its own loop in its own thread. A signal
# handler runs outside every one of those contexts, so the loop mirrors the
# claims here. Written under the lock by the thread that runs the task; the
# signal handler only copies it, because a signal can land while the lock is
# held on the very thread that would wait for it.
_in_flight: dict[int, TaskClaim] = {}
_in_flight_lock = threading.Lock()

# The adaptive limit of a worker running several tasks, for worker_status;
# None while it runs one at a time.
_task_limit: int | None = None


# Kept as a named constant so the test that pins this statement asserts against
# the statement itself: _graceful_exit calls os._exit(), so a test can never
# reach it, and a second copy in the test file would go green while this one
# drifted.
_REQUEUE_ON_EXIT_SQL = (
    "UPDATE tasks SET status = 'pending', attempts = GREATEST(attempts - 1, 0), "
    "started_at = NULL, last_heartbeat = NULL "
    "WHERE id = %s AND status = 'running' AND worker = %s AND attempts = %s"
)


def _graceful_exit(signum: int, frame: Any) -> None:
    """Deploys must not leave in-flight tasks in 'running' limbo until the
    reaper times out: requeue every one immediately (chunks resume from cached
    verdicts) without burning an attempt, then exit. Not waited on: a pull can
    run half an hour and a deploy's stop grace is seconds, so a task left to
    finish is a task killed mid-write.

    Guarded by each claim: a deploy is exactly when a worker is most likely to
    have already lost a task to the reaper, and requeueing then would hand
    back a run another worker is midway through - while crediting it an attempt
    it never spent. One failed requeue leaves that task to the reaper and does
    not stop the others.
    """
    _release_all()
    os._exit(0)


def _release_all() -> None:
    for claim in _in_flight.copy().values():
        try:
            db.execute(_REQUEUE_ON_EXIT_SQL, (claim.task_id, claim.worker, claim.attempts))
            logger.info(f"SIGTERM: requeued task {claim.task_id}")
        except Exception:  # noqa: S110 - nothing may block exit, not even logging
            pass


# How often a busy worker proves it is alive.
#
# Bounded above by the two things that read the answer: the admin fleet view
# calls a worker dead after 90 seconds without a beat, and the reaper requeues
# a task after task_heartbeat_timeout_minutes without one. Sixty seconds keeps
# the screen truthful with a beat of slack, and puts fifteen beats inside the
# default reaper window so losing several in a row still cannot orphan a live
# task. The config refuses a window under two beats.
HEARTBEAT_SECONDS = 60

# Captured at import, so the status upsert reports when this process came up,
# not when the row was first inserted: a roll must show as a new start.
_PROCESS_STARTED_AT = datetime.datetime.now(datetime.UTC)


def _report_worker_status() -> None:
    """Every task this worker holds, oldest first. current_task_id is the
    first of them, so a reader that asks only "idle or not" is unchanged."""
    held = sorted(_in_flight.copy())
    try:
        db.execute(
            """
            INSERT INTO worker_status
                (name, started_at, current_task_id, current_task_ids, task_limit, last_seen,
                 kinds, excluded_kinds, release, egress_group, data_level, data_level_release)
            VALUES (%(name)s, %(started)s, %(tid)s, %(tids)s, %(limit)s, now(), %(kinds)s,
                    %(excluded)s, %(release)s, %(egress)s, %(level)s, %(release)s)
            ON CONFLICT (name) DO UPDATE SET
                started_at = EXCLUDED.started_at,
                current_task_id = EXCLUDED.current_task_id,
                current_task_ids = EXCLUDED.current_task_ids,
                task_limit = EXCLUDED.task_limit, last_seen = now(),
                kinds = EXCLUDED.kinds, excluded_kinds = EXCLUDED.excluded_kinds,
                release = EXCLUDED.release, egress_group = EXCLUDED.egress_group,
                data_level = EXCLUDED.data_level,
                data_level_release = EXCLUDED.data_level_release
            """,
            {
                "name": WORKER_NAME,
                "egress": hosts.EGRESS_GROUP,
                "started": _PROCESS_STARTED_AT,
                "release": telemetry.RELEASE,
                "level": data_level.LEVEL,
                "tid": held[0] if held else None,
                "tids": held,
                "limit": _task_limit,
                # Reported rather than inferred: the filters live in this host's
                # environment, so nothing else can know what this worker refuses.
                "kinds": WORKER_KINDS,
                "excluded": EXCLUDE_KINDS,
            },
        )
    except Exception:
        logger.exception("worker status report failed")


async def _run_held(task: dict[str, Any], claim: TaskClaim) -> bool:
    return await worker_execution.run_held(
        task, claim, WORKER_NAME, _report_worker_status, HEARTBEAT_SECONDS
    )


async def run_once() -> bool:
    task = _claim_task()
    if not task:
        _report_worker_status()
        return False
    await run_task(task)
    return True


async def run_task(task: dict[str, Any]) -> bool:
    """Runs one claimed task to its end. True when it ended on the host running
    out of memory or threads, which a worker running several tasks backs off
    on; every other ending is the task's own."""
    claim = TaskClaim(task["id"], task["worker"], task["attempts"])
    with _in_flight_lock:
        _in_flight[claim.task_id] = claim
    set_current_claim(claim)
    try:
        return await _run_held(task, claim)
    finally:
        set_current_claim(None)
        with _in_flight_lock:
            _in_flight.pop(claim.task_id, None)


# Kinds that run in a thread of their own, on an event loop of their own,
# beside other tasks when worker_task_slots gives this worker more than one.
# Every other kind runs on the main loop, one at a time, as it always has,
# while the threaded ones carry on beside it.
#
# A thread, not a coroutine on the shared loop: handlers call the database
# synchronously, so on a shared loop every statement of one task stalls all
# the others (on oci, 103 ms a round trip, an ingest averaged 38 s and was
# mostly that), and match_mail and a mail backfill never await at all. A loop
# of its own is also why a kind must be audited before it is listed: a module
# global bound to the loop that made it (core.batch._batch_client, an
# AsyncOpenAI client) breaks when a second loop uses it.
#
# ingest_source, audited 2026-10-11: no model call and no loop-bound global
# (its fetches are requests and selenium in to_thread, its writes go through
# the thread-safe pool and keep catalog's lock order); host pacing is shared
# process state and is locked (core.fetching.client.pace); hosts.take is one
# UPDATE, so two pulls of one host on one worker get one slot. It is also
# the volume: 8,388 of the 24 hours' tasks to 2026-10-11 01:45 UTC, 3,441
# pending, p50 1 s and p90 17 s but 219 runs over 5 minutes holding the
# worker for 41,000 s between them, most of it waiting on boards.
THREADED_KINDS = frozenset({"ingest_source"})


class _Slots:
    """Several tasks at once on one worker, under an AIMD limit
    (tasks.runtime.AdaptiveLimiter): it grows by one while a window of
    completions comes faster than the last, and halves when a task fails on
    the host running out of memory or threads, or when free memory falls under
    worker_memory_reserve_mb. A task's weight (worker_task_weights) is the
    slots it takes. Nothing is claimed beyond the limit, nothing but the first
    task is claimed without the reserve free, and a worker holding nothing
    always claims one, so the floor is the worker as it was.

    Only the main thread touches this; a task's thread reports its end on
    the queue."""

    def __init__(self, start: int = 1) -> None:
        self.limiter = AdaptiveLimiter(max_c=start, gauge=None)
        self.limiter.limit = start
        self.weights: dict[str, int] = {}
        self.reserve_mb = 0
        self.held: dict[int, int] = {}
        self.ended: SimpleQueue[tuple[int, bool]] = SimpleQueue()
        self.wake = threading.Event()

    def configure(self, ceiling: int, weights: dict[str, int], reserve_mb: int) -> None:
        self.limiter.max_c = ceiling
        self.limiter.limit = min(self.limiter.limit, ceiling)
        self.weights = weights
        self.reserve_mb = reserve_mb

    def step(self, runner: asyncio.Runner) -> None:
        """One pass: settle what ended, then claim one task if there is room,
        else wait for a task to end or a poll to pass."""
        global _task_limit
        while not self.ended.empty():
            task_id, exhausted = self.ended.get()
            self.held.pop(task_id, None)
            self.limiter.record(rate_limited=exhausted)
        free = available_memory_mb()
        short = free is not None and free < self.reserve_mb
        if short and self.held:
            self.limiter.record(rate_limited=True)
        _task_limit = self.limiter.limit
        room = self.limiter.limit - sum(self.held.values())
        task = None
        if not self.held:
            task = _claim_task()
        elif room > 0 and not short:
            task = _claim_task([k for k, w in self.weights.items() if w > room])
        if task is None:
            _report_worker_status()
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()
            return
        if task["kind"] not in THREADED_KINDS:
            self.limiter.record(rate_limited=runner.run(run_task(task)))
            return
        self.held[task["id"]] = self.weights.get(task["kind"], 1)
        threading.Thread(
            target=self._run, args=(task,), name=f"task-{task['id']}", daemon=True
        ).start()

    def _run(self, task: dict[str, Any]) -> None:
        exhausted = False
        try:
            exhausted = asyncio.run(run_task(task))
        except BaseException:
            # run_task settles every Exception itself; what reaches here is
            # the loop itself failing. The claim stays running with its beat
            # stopped, so the reaper requeues it.
            logger.exception(f"Task {task['id']} thread ended abnormally")
        finally:
            self.ended.put((task["id"], exhausted))
            self.wake.set()


def _task_ceiling() -> int:
    """This worker's ceiling, bounded by its connection pool: each running task
    can hold a connection and its heartbeat another. JOBTRACKER_TASK_SLOTS is
    the host's own value; a worker_task_slots entry for this worker is the
    administrator's override, and the smaller of the two wins, so the admin
    page can lower a host without a deploy but cannot raise it past what the
    host was sized for."""
    named = (db.get_config("worker_task_slots") or {}).get(WORKER_NAME.lower())
    given = [int(v) for v in (TASK_SLOTS, named) if v is not None]
    return max(1, min(min(given, default=1), pool.MAX_SIZE // 2))


def _refresh_slots(slots: _Slots | None) -> tuple[int, _Slots | None]:
    """Read the ceiling and the slot settings; make the slots the first time
    the ceiling is above one. With JOBTRACKER_TASK_SLOTS set they start at the
    ceiling, and the memory guard halves them from there."""
    ceiling = _task_ceiling()
    if slots is None and ceiling > 1:
        slots = _Slots(ceiling if TASK_SLOTS is not None else 1)
    if slots is not None:
        slots.configure(
            ceiling,
            db.get_config("worker_task_weights") or {},
            int(db.get_config("worker_memory_reserve_mb")),
        )
    return ceiling, slots


def _seed_gauges() -> None:
    """A gauge is a process value: it read 0 on every worker from each of
    eight restarts on 2026-09-05 until the hourly detector ran, with 19
    alerts open the whole day. The database is the truth; read it once."""
    row = db.query_one("SELECT COUNT(*) AS n FROM health_alerts WHERE resolved_at IS NULL")
    metrics.HEALTH_ALERTS.set(row["n"] if row else 0)


def main() -> None:
    global _task_limit
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _graceful_exit)
    signal.signal(signal.SIGINT, _graceful_exit)
    db.init_schema()
    db.execute(
        "INSERT INTO worker_status (name) VALUES (%s) ON CONFLICT (name) DO UPDATE "
        "SET started_at = now(), current_task_id = NULL, last_seen = now()",
        (WORKER_NAME,),
    )
    metrics.serve()
    _seed_gauges()
    telemetry.init("jobtracker-worker", WORKER_NAME)
    ingest_enabled = os.environ.get("JOBTRACKER_INGEST_SCHEDULER", "1") == "1"
    logger.info(
        f"Worker started (kinds={WORKER_KINDS or 'all'}, "
        f"excluding={EXCLUDE_KINDS or 'nothing'}, "
        f"scheduler={'on' if ingest_enabled else 'off'})"
    )
    # A restart is an event: a worker that starts every few minutes is a
    # crash loop, and the release it started on is what a regression is
    # measured against.
    telemetry.capture(
        "worker_started",
        properties={
            "worker": WORKER_NAME,
            "kinds": WORKER_KINDS,
            "excluded_kinds": EXCLUDE_KINDS,
            "scheduler": ingest_enabled,
        },
    )
    # Provider clients retain pooled connections across tasks. Their loop must
    # live as long as the worker, not be closed after each run_once call.
    with asyncio.Runner() as runner:
        last_housekeeping = 0.0
        ceiling = 1
        slots: _Slots | None = None
        while True:
            if time.monotonic() - last_housekeeping > 60:
                last_housekeeping = time.monotonic()
                try:
                    reap_stale_tasks()
                    reconcile_chunks()
                    metrics.refresh_queue_gauges()
                    if ingest_enabled:
                        schedule_ingest_cycle()
                except Exception:
                    logger.exception("housekeeping failed")
                try:
                    ceiling, slots = _refresh_slots(slots)
                except Exception:
                    logger.exception("reading task slots failed")
            # One at a time, the loop as it was, unless JOBTRACKER_TASK_SLOTS or
            # worker_task_slots gives this worker more; switched back, it
            # drains what it holds first.
            if slots is None or (ceiling <= 1 and not slots.held):
                slots, _task_limit = None, None
                if not runner.run(run_once()):
                    time.sleep(POLL_SECONDS)
            else:
                slots.step(runner)


# The container entrypoint is `python -m api.worker`. Without this the module
# imports, defines main(), reaches EOF and exits 0 - a clean exit that reads as
# success to every healthcheck, restart policy and metric, while the fleet does
# no work at all. #142 dropped it during the tasks/ split and it went unnoticed
# because the running containers predated that deploy; it surfaced only when CD
# finally caught up. tests/test_worker_entrypoint.py exists to make that
# impossible to repeat.
if __name__ == "__main__":
    main()
