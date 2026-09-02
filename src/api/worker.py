"""The worker loop: claim a task, run its handler, reap what died.

Handlers live in api/tasks/. This module owns only the loop, the queue
mechanics, and the schedule - so a handler can be read without reading the
runtime, and the runtime without reading twelve handlers.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import signal
import socket
import time
from typing import Any

from api import db, events, metrics
from api.tasks import HANDLERS
from api.tasks.runtime import (
    CHUNK_KINDS,
    HEARTBEAT_TIMEOUT_MINUTES,
    MAX_ATTEMPTS,
    AwaitingBatch,
    TaskClaim,
    _finish,
    _maybe_finalize_parent,
    _reconcile_chunks,
    enqueue,
    set_current_claim,
)

logger = logging.getLogger("jobtracker_worker")


POLL_SECONDS = float(os.environ.get("JOBTRACKER_WORKER_POLL", "5"))


INGEST_INTERVAL_MINUTES = int(os.environ.get("JOBTRACKER_INGEST_INTERVAL_MINUTES", "60"))


# Which task kinds this worker claims; lets small fleet hosts (e.g. an rpi)
# opt out of scrape-heavy work. Default: all kinds.
WORKER_KINDS = [
    k.strip() for k in os.environ.get("JOBTRACKER_WORKER_KINDS", "").split(",") if k.strip()
]


# Stamped on every claimed task so the admin UI can attribute work (and
# failures - e.g. one host's IP getting blocked) to a fleet host. Set it in
# compose; the container hostname fallback is a random hex id.
WORKER_NAME = os.environ.get("JOBTRACKER_WORKER_NAME") or socket.gethostname()


def _claim_task() -> dict[str, Any] | None:
    kinds_clause = "AND kind = ANY(%(kinds)s)" if WORKER_KINDS else ""
    return db.query_one(
        f"""
        UPDATE tasks SET status = 'running', started_at = now(),
                         last_heartbeat = now(), attempts = attempts + 1,
                         worker = %(worker)s
        WHERE id = (SELECT id FROM tasks WHERE status = 'pending' {kinds_clause}
                    ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING id, kind, payload, attempts, worker
        """,
        {"kinds": WORKER_KINDS, "worker": WORKER_NAME},
    )


def reap_stale_tasks() -> None:
    """Recover tasks whose worker died mid-run (deploy, crash, OOM): heartbeat
    goes stale -> requeue up to MAX_ATTEMPTS, then fail permanently."""
    requeued = db.execute_count(
        f"""
        UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL
        WHERE status = 'running' AND attempts < {MAX_ATTEMPTS}
          AND COALESCE(last_heartbeat, started_at) < now() - interval '{HEARTBEAT_TIMEOUT_MINUTES} minutes'
        """
    )
    if requeued:
        metrics.REAPER_REQUEUES.inc(requeued)
    db.execute(
        f"""
        UPDATE tasks SET status = 'failed', finished_at = now(),
                         error = 'worker lost (heartbeat timeout after ' || attempts || ' attempts)',
                         payload = COALESCE(payload, '{{}}'::jsonb) - 'batch_ids'
        WHERE status = 'running' AND attempts >= {MAX_ATTEMPTS}
          AND COALESCE(last_heartbeat, started_at) < now() - interval '{HEARTBEAT_TIMEOUT_MINUTES} minutes'
        """
    )


def schedule_ingest_cycle() -> None:
    """Leaderless hourly scheduler: every worker calls this each poll; the
    dedupe key (source + time bucket) guarantees one task per source per cycle
    across the whole fleet."""
    now = datetime.datetime.now(datetime.UTC)
    bucket = now.replace(
        minute=(now.minute // INGEST_INTERVAL_MINUTES) * INGEST_INTERVAL_MINUTES
        if INGEST_INTERVAL_MINUTES < 60
        else 0,
        second=0,
        microsecond=0,
    )
    cycle = bucket.strftime("%Y-%m-%dT%H:%M")
    for s in db.query("SELECT name FROM sources WHERE active"):
        enqueue(
            "ingest_source",
            {"source": s["name"], "cycle": cycle},
            dedupe_key=f"ingest:{s['name']}:{cycle}",
        )
    day = now.strftime("%Y-%m-%d")
    enqueue("reverify_open", {"cycle": day}, dedupe_key=f"reverify:{day}")
    # Hourly, but only when the previous pass has finished. Each run is capped
    # at EXTRACT_COMP_PER_CYCLE jobs and then waits on the Batch API, which can
    # take hours - enqueuing unconditionally every hour would stack passes up
    # until all three workers were doing nothing else. The dedupe key stops two
    # tasks per cycle; this stops overlap ACROSS cycles.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'extract_comp' "
        "AND status IN ('pending', 'running', 'waiting', 'awaiting_batch') LIMIT 1"
    ):
        enqueue("extract_comp", {"cycle": cycle}, dedupe_key=f"comp:{cycle}")
    # Same non-overlap guard, and for the same reason: a capped pass that then
    # parks on the Batch API for hours would otherwise stack a new pass on top
    # of itself every cycle. Kept separate from the comp check so one pass
    # waiting on a slow batch does not block the other from ever starting.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'extract_requirements' "
        "AND status IN ('pending', 'running', 'waiting', 'awaiting_batch') LIMIT 1"
    ):
        enqueue("extract_requirements", {"cycle": cycle}, dedupe_key=f"requirements:{cycle}")
    enqueue("send_digests", {"cycle": day}, dedupe_key=f"digest:{day}")
    enqueue("data_health", {"cycle": cycle}, dedupe_key=f"health:{cycle}")
    enqueue("poll_batches", {"cycle": cycle}, dedupe_key=f"pollbatch:{cycle}")
    # Hourly sweep for jobs the ingest pipeline left unverified (inline AI
    # checks disabled fleet-side): closed+clearance in one batched call each.
    enqueue("verify_new", {"cycle": cycle}, dedupe_key=f"verify:{cycle}")
    # Backlog walker: jobs ingested before content-caching existed (or whose
    # scrape failed) can never be checked until their page is cached.
    enqueue("fetch_missing_content", {"cycle": cycle}, dedupe_key=f"content:{cycle}")


# The claim held by the task currently running on this worker. The handler
# path reads it from the runtime contextvar, but a signal handler runs outside
# that context, so the loop mirrors it here.
_current_claim: TaskClaim | None = None


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
    """Deploys must not leave the in-flight task in 'running' limbo until the
    reaper times out: requeue it immediately (chunks resume from cached
    verdicts) without burning an attempt, then exit.

    Guarded by the claim: a deploy is exactly when a worker is most likely to
    have already lost its task to the reaper, and requeueing then would hand
    back a run another worker is midway through - while crediting it an attempt
    it never spent.
    """
    if _current_claim is not None:
        try:
            db.execute(
                _REQUEUE_ON_EXIT_SQL,
                (_current_claim.task_id, _current_claim.worker, _current_claim.attempts),
            )
            logger.info(f"SIGTERM: requeued task {_current_claim.task_id}, exiting")
        except Exception:  # noqa: S110 - nothing may block exit, not even logging
            pass
    os._exit(0)


# Host resource exhaustion (small fleet hosts hitting their memory ceiling
# while chromium is up). The task is fine; the machine momentarily isn't.
_TRANSIENT_MARKERS = (
    "can't start new thread",
    "cannot allocate memory",
    "resource temporarily unavailable",
    "out of memory",
    "no space left on device",
)


def _is_transient(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)


# Captured at import, so the upsert below reports when this PROCESS came up.
# started_at was previously absent from the ON CONFLICT update list, which left
# it pinned to whenever the row was first inserted - it survived every restart
# and every deploy, so a column named for a start time answered a different
# question entirely, and a roll could look like it had not happened.
_PROCESS_STARTED_AT = datetime.datetime.now(datetime.UTC)


def _report_worker_status(current_task_id: int | None) -> None:
    try:
        db.execute(
            """
            INSERT INTO worker_status (name, started_at, current_task_id, last_seen)
            VALUES (%(name)s, %(started)s, %(tid)s, now())
            ON CONFLICT (name) DO UPDATE SET
                started_at = EXCLUDED.started_at,
                current_task_id = %(tid)s, last_seen = now()
            """,
            {"name": WORKER_NAME, "started": _PROCESS_STARTED_AT, "tid": current_task_id},
        )
    except Exception:
        logger.exception("worker status report failed")


async def run_once() -> bool:
    global _current_claim
    task = _claim_task()
    if not task:
        _report_worker_status(None)
        return False
    claim = TaskClaim(task["id"], task["worker"], task["attempts"])
    _current_claim = claim
    set_current_claim(claim)
    _report_worker_status(task["id"])
    handler = HANDLERS.get(task["kind"])
    events.publish_task(task["id"])
    logger.info(f"Task {task['id']} ({task['kind']}) starting")
    if not handler:
        _finish(task["id"], "failed", f"unknown task kind: {task['kind']}")
        return True
    task_start = time.monotonic()

    async def _liveness() -> None:
        # Progress-based heartbeats stall when every job in flight is slow;
        # this proves the process is alive so the reaper only requeues tasks
        # whose worker actually died. Also keeps worker_status fresh so a
        # host deep in a long chunk never reads as dead.
        while True:
            await asyncio.sleep(60)
            beat = db.execute_count(
                "UPDATE tasks SET last_heartbeat = now() WHERE id = %s AND status = 'running' "
                "AND worker = %s AND attempts = %s",
                (claim.task_id, claim.worker, claim.attempts),
            )
            if not beat:
                # Reaped and re-claimed while we were still working. Beating on
                # it now would vouch for the run that replaced ours.
                logger.warning(f"Task {claim.task_id}: claim lost, stopping heartbeat")
                return
            _report_worker_status(task["id"])

    hb = asyncio.create_task(_liveness())
    try:
        await handler(task["id"], task["payload"])
        _finish(task["id"], "done")
        metrics.TASKS_PROCESSED.labels(task["kind"], "done").inc()
        logger.info(f"Task {task['id']} done")
    except AwaitingBatch:
        # Parked, not finished and not failed: the slot is free and the task
        # resumes when its batches land.
        metrics.TASKS_PROCESSED.labels(task["kind"], "awaiting_batch").inc()
        logger.info(f"Task {task['id']} parked awaiting batches")
    except Exception as exc:
        if _is_transient(exc) and task["attempts"] < MAX_ATTEMPTS:
            # Host ran out of memory/threads, not a broken task: put it back so
            # a healthier worker (or this one, later) takes it. Failing
            # permanently here costs the source a whole ingest cycle.
            db.execute(
                "UPDATE tasks SET status = 'pending', started_at = NULL, "
                "last_heartbeat = NULL, error = %s WHERE id = %s AND status = 'running' "
                "AND worker = %s AND attempts = %s",
                (
                    f"retrying after transient error: {str(exc)[:200]}",
                    claim.task_id,
                    claim.worker,
                    claim.attempts,
                ),
            )
            events.publish_task(task["id"])
            metrics.TASKS_PROCESSED.labels(task["kind"], "requeued").inc()
            logger.warning(f"Task {task['id']} hit a transient error, requeued: {exc}")
        else:
            _finish(task["id"], "failed", str(exc))
            metrics.TASKS_PROCESSED.labels(task["kind"], "failed").inc()
            logger.exception(f"Task {task['id']} failed")
    finally:
        hb.cancel()
        set_current_claim(None)
        _current_claim = None
    metrics.TASK_DURATION.labels(task["kind"]).observe(time.monotonic() - task_start)
    if task["kind"] in CHUNK_KINDS:
        try:
            _maybe_finalize_parent(task["payload"]["parent_id"])
        except Exception:
            logger.exception("parent finalize failed")
    return True


def main() -> None:
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
    ingest_enabled = os.environ.get("JOBTRACKER_INGEST_SCHEDULER", "1") == "1"
    logger.info(
        f"Worker started (kinds={WORKER_KINDS or 'all'}, scheduler={'on' if ingest_enabled else 'off'})"
    )
    last_housekeeping = 0.0
    while True:
        if time.monotonic() - last_housekeeping > 60:
            last_housekeeping = time.monotonic()
            try:
                reap_stale_tasks()
                _reconcile_chunks()
                if ingest_enabled:
                    schedule_ingest_cycle()
            except Exception:
                logger.exception("housekeeping failed")
        worked = asyncio.run(run_once())
        if not worked:
            time.sleep(POLL_SECONDS)


# The container entrypoint is `python -m api.worker`. Without this the module
# imports, defines main(), reaches EOF and exits 0 - a clean exit that reads as
# success to every healthcheck, restart policy and metric, while the fleet does
# no work at all. #142 dropped it during the tasks/ split and it went unnoticed
# because the running containers predated that deploy; it surfaced only when CD
# finally caught up. tests/test_worker_entrypoint.py exists to make that
# impossible to repeat.
if __name__ == "__main__":
    main()
