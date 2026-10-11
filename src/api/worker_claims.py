"""Claim eligible tasks and recover claims left by a lost worker."""

from __future__ import annotations

from typing import Any

from api import db, hosts, metrics, telemetry
from core.env import env_list
from tasks import HANDLERS


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


def claim_task(worker_name: str, too_heavy: list[str] | None = None) -> dict[str, Any] | None:
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
            "worker": worker_name,
            "egress": hosts.EGRESS_GROUP,
        },
    )


def reap_stale_tasks(worker_name: str) -> None:
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
        telemetry.capture("tasks_reaped", properties={"requeued": requeued, "worker": worker_name})
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
        telemetry.capture("tasks_lost", properties={"failed": lost, "worker": worker_name})
