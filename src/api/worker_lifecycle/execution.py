"""Run a held task and settle its completion, parking or failure."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg

from api import ai_pause, db, events, metrics, telemetry
from core.payloads.objects import PayloadUnavailable
from tasks import HANDLERS
from tasks.runtime import (
    CHUNK_KINDS,
    AwaitingBatch,
    Deferred,
    TaskClaim,
    fail_unavailable_payload,
    finish,
    maybe_finalize_parent,
    repark_if_unfinished,
)

logger = logging.getLogger(__name__)

# Infrastructure went away underneath a healthy task. Matched by TYPE, because
# these have real exception classes and substring-matching an error message is
# how the database-restart case was missed: a `pg_ctl restart` severs in-flight
# connections with "terminating connection due to administrator command", which
# was booked as task failure. Two maintenance restarts then exhausted the
# attempt cap and permanently failed work that was merely waiting - stripping
# batch_ids from tasks whose provider batches went on to complete, leaving paid
# results with nothing to consume them.
_TRANSIENT_EXCEPTIONS = (
    psycopg.errors.AdminShutdown,  # 57P01, the restart case
    psycopg.errors.CannotConnectNow,  # 57P03, server starting up
    psycopg.errors.ConnectionException,  # class 08, connection lost mid-statement
    psycopg.OperationalError,  # the pool failing to reconnect at all
    psycopg.InterfaceError,  # connection already closed under us
)

# Host resource exhaustion (small fleet hosts hitting their memory ceiling
# while chromium is up). The task is fine; the machine momentarily isn't.
# These surface as OSError/RuntimeError from the OS with no distinguishing
# type, so they stay string-matched - a narrower use than before, and the only
# one where no type exists to match on instead.
_TRANSIENT_MARKERS = (
    "can't start new thread",
    "cannot allocate memory",
    "resource temporarily unavailable",
    "out of memory",
    "no space left on device",
)


def _task_props(task: dict[str, Any], exc: BaseException, worker_name: str) -> dict[str, Any]:
    """What a failure event carries: enough to group by kind, worker and
    error class, and to find the row. The user a task ran for, when it names
    one, so a user-facing failure can be read per person."""
    payload = task.get("payload") or {}
    return {
        "task_id": task["id"],
        "task_kind": task["kind"],
        "worker": worker_name,
        "attempts": task.get("attempts"),
        "error_class": type(exc).__name__,
        "error": str(exc)[:500],
        "user_id": payload.get("user_id"),
        "source": payload.get("source"),
    }


def _exhausted(exc: BaseException) -> bool:
    """The host ran short, not the task: what a worker running several tasks
    backs off on."""
    return isinstance(exc, MemoryError) or any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, _TRANSIENT_EXCEPTIONS):
        return True
    return any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)


async def run_held(
    task: dict[str, Any],
    claim: TaskClaim,
    worker_name: str,
    report_status: Callable[[], None],
    heartbeat_seconds: float,
) -> bool:
    report_status()
    handler = HANDLERS.get(task["kind"])
    events.publish_task(task["id"])
    logger.info(f"Task {task['id']} ({task['kind']}) starting")
    if not handler:
        finish(task["id"], "failed", f"unknown task kind: {task['kind']}")
        return False
    task_start = time.monotonic()
    exhausted = False

    stop_beating = threading.Event()

    def _liveness() -> None:
        # Progress-based heartbeats stall when every job in flight is slow;
        # this proves the process is alive so the reaper only requeues tasks
        # whose worker actually died. Also keeps worker_status fresh so a host
        # deep in a long chunk never reads as dead.
        #
        # A THREAD, not an asyncio task, and that is the whole point. A handler
        # declared `async def` that never awaits blocks the event loop for its
        # entire run, so a coroutine heartbeat is never scheduled and the
        # liveness signal fails exactly when the work is longest. Two handler
        # modules contained no await at all - mail_match and a mail backfill - and
        # match_mail was measured holding a worker for 428 seconds while the
        # admin fleet view reported it dead the whole time.
        #
        # Liveness must not depend on the thing it is monitoring. A thread
        # beats whether or not the loop is free, which is what a heartbeat is
        # for.
        while not stop_beating.wait(heartbeat_seconds):
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
            report_status()

    hb = threading.Thread(target=_liveness, name="liveness", daemon=True)
    hb.start()
    span_ids: dict[str, str] = {}
    try:
        # One span per task, so a trace of the queue reads as tasks with
        # their board fetches and model calls underneath, and an exception
        # captured inside carries this span's ids.
        with telemetry.task_span(task, worker_name) as ids:
            span_ids.update(ids)
            await handler(task["id"], task["payload"])
        if repark_if_unfinished(task["id"]):
            # The handler collected the batches that had finished and left
            # the rest on its payload: it waits on those, it is not done.
            metrics.TASKS_PROCESSED.labels(task["kind"], "awaiting_batch").inc()
            logger.info(f"Task {task['id']} parked again on unfinished batches")
        else:
            finish(task["id"], "done")
            metrics.TASKS_PROCESSED.labels(task["kind"], "done").inc()
            logger.info(f"Task {task['id']} done")
    except ai_pause.AIPaused as exc:
        # The switch, not a fault: the task ends without its model work and
        # the scheduler enqueues a fresh one once AI is back on.
        finish(task["id"], "done", str(exc))
        metrics.TASKS_PROCESSED.labels(task["kind"], "ai_paused").inc()
        logger.info(f"Task {task['id']} ended: {exc}")
    except AwaitingBatch:
        # Parked, not finished and not failed: the slot is free and the task
        # resumes when its batches land.
        metrics.TASKS_PROCESSED.labels(task["kind"], "awaiting_batch").inc()
        logger.info(f"Task {task['id']} parked awaiting batches")
    except Deferred as d:
        # Back to pending until the host's slot opens, the attempt given
        # back: waiting on a limit is not a failure and must not count as one.
        db.execute(
            "UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL, "
            "worker = NULL, attempts = GREATEST(attempts - 1, 0), not_before = %s "
            "WHERE id = %s AND status = 'running' AND worker = %s",
            (d.not_before, claim.task_id, claim.worker),
        )
        metrics.TASKS_PROCESSED.labels(task["kind"], "deferred").inc()
        logger.info(f"Task {task['id']} {d}")
    except PayloadUnavailable as exc:
        fail_unavailable_payload(task["id"], str(exc))
        metrics.TASKS_PROCESSED.labels(task["kind"], "failed").inc()
        logger.exception("Task %s requires payload restoration before retry", task["id"])
        telemetry.capture_exception(
            exc, properties={**_task_props(task, exc, worker_name), **span_ids}
        )
    except Exception as exc:
        exhausted = _exhausted(exc)
        if _is_transient(exc) and task["retries_spent"] < int(db.get_config("task_max_attempts")):
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
            telemetry.capture(
                "task_requeued", properties={**_task_props(task, exc, worker_name), **span_ids}
            )
        else:
            finish(task["id"], "failed", str(exc))
            metrics.TASKS_PROCESSED.labels(task["kind"], "failed").inc()
            logger.exception(f"Task {task['id']} failed")
            # The traceback and the event both: the traceback groups with
            # its siblings in the error tracker, the event is what a query
            # over failures by kind and worker reads.
            telemetry.capture_exception(
                exc, properties={**_task_props(task, exc, worker_name), **span_ids}
            )
            telemetry.capture(
                "task_failed", properties={**_task_props(task, exc, worker_name), **span_ids}
            )
    finally:
        # Signalled rather than cancelled: a thread cannot be cancelled, and
        # the wait() returns immediately so the join costs nothing. Joined so
        # the beat cannot outlive the claim it vouches for and stamp a task the
        # next loop iteration has already moved on from.
        stop_beating.set()
        hb.join(timeout=5)
    metrics.TASK_DURATION.labels(task["kind"]).observe(time.monotonic() - task_start)
    if task["kind"] in CHUNK_KINDS:
        try:
            maybe_finalize_parent(task["payload"]["parent_id"])
        except Exception:
            logger.exception("parent finalize failed")
    return exhausted
