"""The worker loop: claim a task, run its handler, reap what died.

Handlers live in src/tasks/. This module owns only the loop, the queue
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
import threading
import time
from queue import SimpleQueue
from typing import Any

import psycopg

from api import (
    db,
    events,
    hosts,
    managed_board_runs,
    metrics,
    queue,
    telemetry,
)
from api.board import visibility
from api.mail import match as mail_match
from api.queue import enqueue
from core import pool
from core.env import env_list
from core.fetching.hosts import pace_key
from core.payload_objects import PayloadUnavailable
from tasks import DERIVATIONS, HANDLERS
from tasks.runtime import (
    CHUNK_KINDS,
    AdaptiveLimiter,
    AwaitingBatch,
    Deferred,
    TaskClaim,
    available_memory_mb,
    fail_unavailable_payload,
    finish,
    maybe_finalize_parent,
    reconcile_chunks,
    repark_if_unfinished,
    set_current_claim,
)

logger = logging.getLogger(__name__)


POLL_SECONDS = float(os.environ.get("JOBTRACKER_WORKER_POLL", "5"))


# How often to ask the provider whether parked batches have landed. One
# minute, matching the housekeeping tick, which is the floor: a finer bucket
# cannot fire more often than the loop that reads it.
#
# It shares no bucket with the ingest cycle deliberately. On the hourly one a
# parked task could sit an hour after its batch had finished, and across a
# multi-round backfill that latency exceeds the provider's own turnaround.
#
# Cost is one provider call per OPEN batch and nothing at all when there are
# none, so the frequency is bounded by politeness rather than expense.
BATCH_POLL_MINUTES = int(os.environ.get("JOBTRACKER_BATCH_POLL_MINUTES", "1"))


# Which task kinds this worker claims; lets small fleet hosts (e.g. an rpi, or
# a 1GB free-tier VM that cannot run chromium) opt out of scrape-heavy work.
#
# Two knobs, because they fail in opposite directions and the right one depends
# on WHY a host is limited:
#
#   JOBTRACKER_WORKER_KINDS         allowlist - claim ONLY these
#   JOBTRACKER_WORKER_EXCLUDE_KINDS denylist  - claim everything EXCEPT these
#
# An allowlist fails closed: add a kind next month and a host pinned to an
# allowlist silently never runs it. That has bitten this fleet twice, which is
# why kanishk-desktop was capped with concurrency limits instead. A denylist
# fails open: a new kind runs everywhere by default and only the named ones are
# excluded. Prefer the denylist for a host limited by hardware, the allowlist
# for a host deliberately pinned to one job.
#
# Both set is not an error and not a precedence fight: the allowlist restricts
# and the denylist then subtracts, so the result is what both agree on. Order
# of evaluation cannot change the answer, which is the property that makes it
# safe to set both without reasoning about which wins.
def _kinds_clause(kinds: list[str], exclude: list[str]) -> str:
    """The WHERE fragment that narrows what this worker will claim."""
    clause = "AND kind = ANY(%(kinds)s)" if kinds else ""
    if exclude:
        clause += " AND NOT (kind = ANY(%(exclude)s))"
    return clause


WORKER_KINDS = env_list("JOBTRACKER_WORKER_KINDS")
EXCLUDE_KINDS = env_list("JOBTRACKER_WORKER_EXCLUDE_KINDS")


# Stamped on every claimed task so the admin UI can attribute work (and
# failures - e.g. one host's IP getting blocked) to a fleet host. Set it in
# compose; the container hostname fallback is a random hex id.
WORKER_NAME = os.environ.get("JOBTRACKER_WORKER_NAME") or socket.gethostname()

_managed_board_schedule_attempts: set[tuple[str, int]] = set()


# The retry budget, which is not the claim count. Every claim increments
# attempts, because (worker, attempts) is the generation stamp a claim's
# writes are guarded by and a repeated stamp would let a lost worker write
# over the run that replaced it. A claim that resumes a parked batch wait is
# not a retry, so resume_parked counts it in batch_resumes and the budget
# subtracts it. Counting resumes against the budget let 404 batch tasks reach
# the cap in the 30 days to 2026-10-04 without a single failure, and two of
# them were then failed at their first real error with 1,198 paid receipts
# unconsumed.
RETRIES_SPENT = "(attempts - batch_resumes)"


# The order eligible tasks are claimed in: by age, except that a scheduled
# source pull counts as created one of its source's own intervals later.
#
# A scheduled pull is the task whose payload names a host (the key api.hosts
# paces on); the hourly scheduler makes one per active source at once, about
# hundreds of them, and nothing else fans out like that. Every other kind is
# at most one pending task per key. Plain id order put each burst of pulls
# ahead of whatever came after it: on 2026-10-10 a poll_batches created at
# 05:11 waited 95 minutes behind 240 pulls and 115 board recomputes, so paid
# batches sat uncollected.
#
# The shift is the source's ingest_interval_hours: a pull that waits that long
# has missed one pull of its own, and the scheduler makes no second one while
# it is pending, so nothing stacks. It bounds the wait too: a pull created at T
# is claimed before any task created after T plus its interval. A source pull
# a person asked for carries no host and is not shifted.
#
# A scalar subquery, not a join, so FOR UPDATE locks only the task row and
# never a sources row that admission locks.
CLAIM_ORDER = """
    created_at + CASE WHEN payload ? 'host' THEN COALESCE(
        (SELECT make_interval(hours => s.ingest_interval_hours) FROM sources s
          WHERE s.name = tasks.payload->>'source'), interval '0') ELSE interval '0' END,
    id"""


def _claim_task(too_heavy: list[str] | None = None) -> dict[str, Any] | None:
    """Only kinds this image has a handler for. A roll goes host by host, and
    a host still on the old image sees the new kind a rolled host enqueued:
    on 2026-09-05 gcp-vps claimed the first classify_locations task and
    failed it terminally as an unknown kind, in the seconds before its own
    deploy. A kind this worker cannot run waits for one that can.

    `too_heavy` names kinds that need more slots than a worker running several
    tasks has free; they wait for a worker, or a moment, with the room."""
    exclude = EXCLUDE_KINDS + (too_heavy or [])
    kinds_clause = _kinds_clause(WORKER_KINDS, exclude)
    return db.query_one(
        f"""
        UPDATE tasks SET status = 'running', started_at = now(),
                         last_heartbeat = now(), attempts = attempts + 1,
                         worker = %(worker)s
        WHERE id = (SELECT id FROM tasks WHERE status = 'pending'
                      AND kind = ANY(%(known)s) {kinds_clause}
                      -- Not before its time, and not against a host whose
                      -- slot for this address is still closed (api.hosts).
                      AND (not_before IS NULL OR not_before <= now())
                      AND NOT EXISTS (
                          SELECT 1 FROM host_budget b
                          WHERE b.host = tasks.payload->>'host'
                            AND b.egress_group = %(egress)s
                            AND b.next_allowed_at > now())
                    ORDER BY {CLAIM_ORDER} LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING id, kind, payload, attempts, worker, {RETRIES_SPENT} AS retries_spent
        """,
        {
            "known": list(HANDLERS),
            "kinds": WORKER_KINDS,
            "exclude": exclude,
            "worker": WORKER_NAME,
            "egress": hosts.EGRESS_GROUP,
        },
    )


def reap_stale_tasks() -> None:
    """Recover tasks whose worker died mid-run (deploy, crash, OOM): heartbeat
    goes stale -> requeue up to task_max_attempts, then fail permanently.

    A task failed here keeps its batch_ids, for the reason finish() keeps a
    failed task's: the batches they name were never collected."""
    limits = {
        "attempts": int(db.get_config("task_max_attempts")),
        "minutes": int(db.get_config("task_heartbeat_timeout_minutes")),
    }
    requeued = db.execute_count(
        f"""
        UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL
        WHERE status = 'running' AND {RETRIES_SPENT} < %(attempts)s
          AND COALESCE(last_heartbeat, started_at) < now() - make_interval(mins => %(minutes)s)
        """,
        limits,
    )
    if requeued:
        metrics.REAPER_REQUEUES.inc(requeued)
        # A reaped task is a worker that died mid-run, which no exception
        # handler on that worker could record. The reaper is the only witness.
        telemetry.capture("tasks_reaped", properties={"requeued": requeued, "worker": WORKER_NAME})
    lost = db.execute_count(
        f"""
        UPDATE tasks SET status = 'failed', finished_at = now(),
                         error = 'worker lost (heartbeat timeout after '
                                 || {RETRIES_SPENT} || ' attempts)'
        WHERE status = 'running' AND {RETRIES_SPENT} >= %(attempts)s
          AND COALESCE(last_heartbeat, started_at) < now() - make_interval(mins => %(minutes)s)
        """,
        limits,
    )
    if lost:
        telemetry.capture("tasks_lost", properties={"failed": lost, "worker": WORKER_NAME})


def schedule_ingest_cycle() -> None:
    """Leaderless hourly scheduler: every worker calls this each poll; the
    dedupe key (source + time bucket) guarantees one task per source per cycle
    across the whole fleet."""
    now = datetime.datetime.now(datetime.UTC)
    interval = int(db.get_config("ingest_interval_minutes"))
    bucket = now.replace(
        minute=(now.minute // interval) * interval if interval < 60 else 0,
        second=0,
        microsecond=0,
    )
    cycle = bucket.strftime("%Y-%m-%dT%H:%M")
    # One set-based query, then ON CONFLICT inserts; the per-cycle dedupe key
    # is what makes this race-safe across the fleet. It must stay one query:
    # every worker runs this on EVERY poll, and #483 (2026-09-09) replaced it
    # with one locked transaction per active source (FOR UPDATE on the source
    # row plus four round trips), 389 of them a poll. Beside the database that
    # took a second; on oci, transmission, desktop and gcp-vps at 100 to
    # 200 ms a round trip it took minutes, the loop began the next poll's
    # pass as soon as it finished, and four of six workers never reached
    # worker_status or a claim again, with healthy containers and clean
    # logs. task_admission.enqueue stays for the request-triggered kinds,
    # which run once per request, not per poll.
    #
    # A source whose last cycle's ingest has not been claimed yet gets no
    # second one. Without this a queue that falls behind the hour grows by
    # one task per source per hour and never catches up: 69 boards added at
    # once on 2026-09-04 were still pending 40 minutes later behind two
    # aggregator ingests, with the next cycle due to add 79 more. A RUNNING
    # ingest does not block the next cycle's task, because that one will be
    # claimed as soon as it finishes.
    #
    # A source on a longer interval than the cycle is skipped while its last
    # successful or in-flight ingest is younger than that interval. Hourly
    # sources (interval 1) are governed by the per-cycle dedupe alone, which
    # is exact where an age check drifts.
    #
    # A failed pull counts toward the interval too, and a run of them backs
    # off (queue.backing_off). It used to retry next cycle: twelve boards,
    # most answering 404, were pulled every hour for up to eleven days, and
    # every board together failed 1,509 pulls in the week to 2026-10-04. Asked
    # in a second query of only the sources this one selects, because over
    # every source it is 0.68 s, every minute, on every worker.
    due = db.query(
        """
        SELECT name, listings_url FROM sources s WHERE active
          AND NOT EXISTS (
            SELECT 1 FROM tasks t WHERE t.kind = 'ingest_source' AND t.status = 'pending'
              AND t.payload->>'source' = s.name)
          AND (s.ingest_interval_hours <= 1 OR NOT EXISTS (
            SELECT 1 FROM tasks t WHERE t.kind = 'ingest_source'
              AND t.status IN ('running', 'done')
              AND t.payload->>'source' = s.name
              AND t.created_at > now() - make_interval(hours => s.ingest_interval_hours)))
        """
    )
    waiting = queue.backing_off([s["name"] for s in due])
    for s in due:
        if s["name"] in waiting:
            continue
        enqueue(
            "ingest_source",
            {"source": s["name"], "cycle": cycle, "host": pace_key(s["listings_url"])},
            dedupe_key=f"ingest:{s['name']}:{cycle}",
        )
    # The other half of the query above: a source it no longer selects is
    # never pulled, so nothing else would retire its postings. A no-op once
    # the catalog agrees (catalog.retire_switched_off).
    enqueue("retire_switched_off", {"cycle": cycle}, dedupe_key=f"retire-off:{cycle}")
    # Messages whose current event or match pointer lags its log
    # (tasks.mail_pointers). Counting them is a pass over every message,
    # 0.9 s on production on 2026-10-10, so the task counts once a cycle
    # rather than every worker on every poll; a run with none to fill only
    # reads. One at a time.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'backfill_mail_pointers' "
        "AND status IN ('pending', 'running') LIMIT 1"
    ):
        enqueue("backfill_mail_pointers", {"cycle": cycle}, dedupe_key=f"mail-pointers:{cycle}")
    # The .olm copies of messages Takeout also holds (tasks.mail_olm_twins):
    # one run does all of it in one transaction, so it is offered until one
    # has finished.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'merge_olm_twins' "
        "AND status IN ('pending', 'running', 'done') LIMIT 1"
    ):
        enqueue("merge_olm_twins", {"cycle": cycle}, dedupe_key=f"olm-twins:{cycle}")
    # Older answers pointed at their page fetch and call (tasks.answer_links),
    # one run at a time, until a run finishes having linked nothing.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'link_answers' "
        "AND (status IN ('pending', 'running') "
        "     OR (status = 'done' AND progress->>'total' = '0')) LIMIT 1"
    ):
        enqueue("link_answers", {"cycle": cycle}, dedupe_key=f"answer-links:{cycle}")
    # The copies answers carried are emptied (tasks.answer_copies), one run at
    # a time, until a run clears nothing; then the release after drops them.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'clear_answer_copies' "
        "AND (status IN ('pending', 'running') "
        "     OR (status = 'done' AND progress->>'total' = '0')) LIMIT 1"
    ):
        enqueue("clear_answer_copies", {"cycle": cycle}, dedupe_key=f"answer-copies:{cycle}")
    # Board membership for every person who can have one, every
    # board_refresh_minutes, so new verdicts reach a board without anyone
    # touching a preference. Bucketed like the ingest cycle; a person's own
    # preference write asks sooner. A users row with no subscription, no
    # board row and no upload admits nothing under the predicate, and one
    # such row (a probe that signed in once) drew 825 full recomputes in a
    # day, 60 percent of the real person's, each producing zero rows.
    #
    # A person with a recompute already pending gets no second one: the
    # pending one reads everything as of when it runs, so a second has nothing
    # of its own to do. Without this a queue that falls behind grows by one
    # per person per bucket; on 2026-10-10 66 were stacked for three people.
    # A running one does not block, because it may have read what changed.
    refresh = max(1, int(db.get_config("board_refresh_minutes")))
    rbucket = now.replace(minute=(now.minute // refresh) * refresh, second=0, microsecond=0)
    rcycle = rbucket.strftime("%Y-%m-%dT%H:%M")
    for u in db.query(
        f"""
        SELECT id FROM users u
        WHERE (EXISTS (SELECT 1 FROM user_source_set s WHERE s.user_id = u.id)
               OR EXISTS (SELECT 1 FROM user_jobs j WHERE j.user_id = u.id)
               OR EXISTS (SELECT 1 FROM posting_uploads p WHERE p.uploaded_by = u.id))
          AND NOT {visibility.RECOMPUTE_PENDING}
        ORDER BY id
        """
    ):
        enqueue(
            "recompute_board",
            {"user_id": u["id"], "cycle": rcycle},
            dedupe_key=f"board:{u['id']}:{rcycle}",
        )
    _managed_board_schedule_attempts.intersection_update(
        {key for key in _managed_board_schedule_attempts if key[0] == cycle}
    )
    for board in db.query("SELECT id FROM managed_boards WHERE published ORDER BY id"):
        attempt = (cycle, board["id"])
        if attempt in _managed_board_schedule_attempts:
            continue
        try:
            managed_board_runs.admit(board["id"], dedupe_key=f"managed-board:{board['id']}:{cycle}")
        except managed_board_runs.RunRefusal as exc:
            logger.info(
                "Managed board %s not scheduled: %s (%s)",
                board["id"],
                exc.message,
                exc.code,
            )
            telemetry.capture(
                "managed_board_schedule_refused",
                properties={"managed_board_id": board["id"], "code": exc.code},
            )
        _managed_board_schedule_attempts.add(attempt)
    # Application answers ahead of need, hourly, for each person who has put
    # a resume in: the forms of the postings on their board are read and
    # every question without a draft rides one half-price batch, so the
    # answer is there when the posting is opened. See tasks.application.
    for u in db.query("SELECT DISTINCT user_id AS id FROM user_resumes ORDER BY 1"):
        enqueue(
            "application_sweep",
            {"user_id": u["id"], "cycle": cycle},
            dedupe_key=f"appsweep:{u['id']}:{cycle}",
        )
    day = now.strftime("%Y-%m-%d")
    enqueue("reverify_open", {"cycle": day}, dedupe_key=f"reverify:{day}")
    enqueue("sync_gmail", {"cycle": cycle}, dedupe_key=f"gmail:{cycle}")
    # Importing mail nobody classifies is an inbox, not an ingest. The sync
    # above runs hourly, so without these two the pipeline would fill
    # email_messages forever and derive nothing from it.
    #
    # Both carry the same cross-cycle guard as the batched extractions: each
    # parks on the Batch API and can outlive its own cycle, so the dedupe key
    # alone would stack a pass an hour on top of one still waiting. Separate
    # checks so a slow classification does not stop matching from running -
    # matching is cheap and re-runs improve on themselves as new board rows
    # appear, which is exactly when it should not be blocked.
    if db.get_config("mail_classification_enabled") and not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'classify_mail' "
        "AND status IN ('pending', 'running', 'waiting', 'awaiting_batch') LIMIT 1"
    ):
        enqueue("classify_mail", {"cycle": cycle}, dedupe_key=f"mailclassify:{cycle}")
    # And only when something a sweep reads has changed since the last one
    # started: otherwise it would decide everything exactly as before.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'match_mail' "
        "AND status IN ('pending', 'running', 'waiting') LIMIT 1"
    ) and mail_match.changed_since(mail_match.last_sweep_start()):
        enqueue("match_mail", {"cycle": cycle}, dedupe_key=f"mailmatch:{cycle}")
    # Its own kind and its own key, NOT folded into the sync. Dead-credential
    # detection is discovery-on-use, so if it only happened inside the sync
    # then a sync that stops running also stops noticing it cannot run - the
    # alarm wired to the thing it is alarming about.
    enqueue("probe_credentials", {"cycle": cycle}, dedupe_key=f"credprobe:{cycle}")
    # Every derived fact (pay, requirements, job profiles, embeddings,
    # locations): one pass each, only when switched on and none is in flight.
    for derivation in DERIVATIONS:
        derivation.schedule(cycle)
    enqueue("send_digests", {"cycle": day}, dedupe_key=f"digest:{day}")
    enqueue("data_health", {"cycle": cycle}, dedupe_key=f"health:{cycle}")
    # Polling gets its OWN bucket, far finer than the ingest cycle. Sharing the
    # hourly one meant a parked task could sit up to an hour after its batch
    # had actually finished, and in a multi-round backfill that latency is the
    # dominant term - larger than the provider's own turnaround.
    #
    # The poll costs one provider call per OPEN batch and nothing when there
    # are none, so the frequency is bounded by politeness rather than expense.
    poll_bucket = now.replace(
        minute=(now.minute // BATCH_POLL_MINUTES) * BATCH_POLL_MINUTES,
        second=0,
        microsecond=0,
    ).strftime("%Y-%m-%dT%H:%M")
    # The dedupe key stops two polls per BUCKET; this stops a queue of them
    # across buckets. A poll is idempotent and stateless - it reports on
    # whatever is open right now - so a second one waiting behind the first has
    # nothing of its own to do, and eleven had piled up behind an hour of
    # scraping. That is not merely wasteful: each holds a worker slot when it
    # finally runs, and the thing it is competing with is the collection of
    # batches that have already been paid for.
    #
    # Same distinction as the classify bug: "has it run" and "has it been
    # enqueued" are different questions, and scheduling on the second one
    # re-queues work that is already in flight.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'poll_batches' "
        "AND status IN ('pending', 'running') LIMIT 1"
    ):
        enqueue("poll_batches", {"cycle": poll_bucket}, dedupe_key=f"pollbatch:{poll_bucket}")
    # Hourly sweep for jobs the ingest pipeline left unverified (inline AI
    # checks disabled fleet-side): closed+clearance in one batched call each.
    # Its predicate (no closed/clearance verdict) stays true while a batch is in
    # flight, so a second sweep would re-select the same jobs and pay again;
    # tasks.verify._in_flight excludes what a parked sweep already submitted.
    # A parked sweep therefore does not block the next one: a straggling batch
    # held the only sweep for 15 hours on 2026-10-08, and no new posting
    # reached a board in that time. Only an unparked sweep (pending, running,
    # waiting) does, so two never select at once.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'verify_new' "
        "AND status IN ('pending', 'running', 'waiting') LIMIT 1"
    ):
        enqueue("verify_new", {"cycle": cycle}, dedupe_key=f"verify:{cycle}")
    # Backlog walker: jobs ingested before content-caching existed (or whose
    # scrape failed) can never be checked until their page is cached.
    enqueue("fetch_missing_content", {"cycle": cycle}, dedupe_key=f"content:{cycle}")


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


def _task_props(task: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    """What a failure event carries: enough to group by kind, worker and
    error class, and to find the row. The user a task ran for, when it names
    one, so a user-facing failure can be read per person."""
    payload = task.get("payload") or {}
    return {
        "task_id": task["id"],
        "task_kind": task["kind"],
        "worker": WORKER_NAME,
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


# Captured at import, so the upsert below reports when this PROCESS came up.
# started_at was previously absent from the ON CONFLICT update list, which left
# it pinned to whenever the row was first inserted - it survived every restart
# and every deploy, so a column named for a start time answered a different
# question entirely, and a roll could look like it had not happened.
# How often a busy worker proves it is alive.
#
# Bounded above by the two things that read the answer: the admin fleet view
# calls a worker dead after 90 seconds without a beat, and the reaper requeues
# a task after task_heartbeat_timeout_minutes without one. Sixty seconds keeps
# the screen truthful with a beat of slack, and puts fifteen beats inside the
# default reaper window so losing several in a row still cannot orphan a live
# task. The config refuses a window under two beats.
HEARTBEAT_SECONDS = 60


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
                 kinds, excluded_kinds, release, egress_group)
            VALUES (%(name)s, %(started)s, %(tid)s, %(tids)s, %(limit)s, now(), %(kinds)s,
                    %(excluded)s, %(release)s, %(egress)s)
            ON CONFLICT (name) DO UPDATE SET
                started_at = EXCLUDED.started_at,
                current_task_id = EXCLUDED.current_task_id,
                current_task_ids = EXCLUDED.current_task_ids,
                task_limit = EXCLUDED.task_limit, last_seen = now(),
                kinds = EXCLUDED.kinds, excluded_kinds = EXCLUDED.excluded_kinds,
                release = EXCLUDED.release, egress_group = EXCLUDED.egress_group
            """,
            {
                "name": WORKER_NAME,
                "egress": hosts.EGRESS_GROUP,
                "started": _PROCESS_STARTED_AT,
                "release": telemetry.RELEASE,
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


async def _run_held(task: dict[str, Any], claim: TaskClaim) -> bool:
    _report_worker_status()
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
        while not stop_beating.wait(HEARTBEAT_SECONDS):
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
            _report_worker_status()

    hb = threading.Thread(target=_liveness, name="liveness", daemon=True)
    hb.start()
    span_ids: dict[str, str] = {}
    try:
        # One span per task, so a trace of the queue reads as tasks with
        # their board fetches and model calls underneath, and an exception
        # captured inside carries this span's ids.
        with telemetry.task_span(task, WORKER_NAME) as ids:
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
        telemetry.capture_exception(exc, properties={**_task_props(task, exc), **span_ids})
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
            telemetry.capture("task_requeued", properties={**_task_props(task, exc), **span_ids})
        else:
            finish(task["id"], "failed", str(exc))
            metrics.TASKS_PROCESSED.labels(task["kind"], "failed").inc()
            logger.exception(f"Task {task['id']} failed")
            # The traceback and the event both: the traceback groups with
            # its siblings in the error tracker, the event is what a query
            # over failures by kind and worker reads.
            telemetry.capture_exception(exc, properties={**_task_props(task, exc), **span_ids})
            telemetry.capture("task_failed", properties={**_task_props(task, exc), **span_ids})
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

    def __init__(self) -> None:
        self.limiter = AdaptiveLimiter(max_c=1, gauge=None)
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
    """This worker's worker_task_slots, bounded by its connection pool: each
    running task can hold a connection and its heartbeat another."""
    slots = db.get_config("worker_task_slots") or {}
    return max(1, min(int(slots.get(WORKER_NAME.lower(), 1)), pool.MAX_SIZE // 2))


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
                    ceiling = _task_ceiling()
                    if slots is not None or ceiling > 1:
                        slots = slots or _Slots()
                        slots.configure(
                            ceiling,
                            db.get_config("worker_task_weights") or {},
                            int(db.get_config("worker_memory_reserve_mb")),
                        )
                except Exception:
                    logger.exception("reading task slots failed")
            # One at a time, the loop as it was, unless worker_task_slots names
            # this worker; switched back, it drains what it holds first.
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
