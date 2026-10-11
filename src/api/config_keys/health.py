"""Persisted health settings and their admin metadata."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, PositiveInt

from api.config_types import ConfigKey

CONFIG_KEYS: dict[str, ConfigKey] = {
    # An idle worker beside pending work for this long is a stall: a claim
    # takes one poll (JOBTRACKER_WORKER_POLL, 5s), so ten minutes is not
    # latency. Kinds allowlists (JOBTRACKER_WORKER_KINDS) are the one
    # legitimate reason, and the alert names them.
    "queue_stall_minutes": ConfigKey(
        section="Health",
        default=10,
        value_type=PositiveInt,
        help="Minutes an idle worker may sit beside pending work before the queue counts as stalled.",
    ),
    # Pending ingests older than this many ingest cycles mean the fleet is
    # behind the hour; one cycle is the normal wait.
    "ingest_backlog_cycles": ConfigKey(
        section="Health",
        default=2,
        value_type=PositiveInt,
        help="Pending ingests older than this many hourly cycles mean the fleet is behind.",
    ),
    # A live handler that has not changed its progress for this long is wedged,
    # even when its timer heartbeat remains fresh. Measured 2026-09-11: the
    # completed seven-day sample's p95 finish-after-progress gap was under 0.22
    # minutes and its maximum was 15.4 minutes across sampled kinds. The live
    # match_mail task that looked stopped for 166.5 minutes was not: it wrote
    # progress once per user, and production has one. 128 of the 235 stall
    # alerts in the 30 days to 2026-10-04 were that, and it now writes progress
    # every 100 messages.
    "task_progress_stall_minutes": ConfigKey(
        section="Health",
        default=30,
        value_type=PositiveInt,
        help="Minutes a running task may go without changing its reported progress, counted from its last progress or its current claim, whichever is later, before it counts as stalled.",
    ),
    # A parked task resumes once every batch is terminal, or once the ones
    # still running are older than this while others have finished: the
    # finished ones are collected and the task parks again on the rest.
    # Provider batches normally land within the hour; the stragglers seen
    # on 2026-09-04 sat 14 hours at a few requests short.
    "batch_straggler_hours": ConfigKey(
        section="Health",
        default=4,
        value_type=PositiveInt,
        help="Hours a still-running provider batch may lag its finished siblings "
        "before the task collects those and parks again on it.",
    ),
    # How long GET /admin/stats serves the same answer. The dashboard refetches
    # it on every worker event: 642 calls a day at 583 ms each, five full
    # scans of ai_queries per call when measured (one scan since 2026-10-04),
    # a quarter of all server time on the box for lifetime totals that move
    # once an hour. 1 is as good as off.
    "admin_stats_cache_seconds": ConfigKey(
        section="Health",
        default=60,
        value_type=PositiveInt,
        help="Seconds GET /admin/stats serves the same answer.",
    ),
    # Minutes a worker may heartbeat on a release other than the api's before
    # that is a host that did not deploy. A lockstep roll finishes inside ten,
    # but gcp-vps is deployed by hand until its runner exists, so the window
    # covers a person noticing a roll request; tighten it when every host
    # self-deploys. gcp-vps ran an hour behind on 2026-09-04 unnoticed.
    "fleet_roll_minutes": ConfigKey(
        section="Health",
        default=120,
        value_type=PositiveInt,
        help="Minutes a worker may run a different release from the api before it counts as a "
        "host that did not deploy.",
    ),
    "health_warning_digest_hour_utc": ConfigKey(
        section="Health",
        default=13,
        value_type=Annotated[int, Field(ge=0, le=23)],
        help="UTC hour for the daily warning digest. Critical incidents are sent immediately; "
        "a missed digest is delivered on the next health run. All alerts remain in the UI.",
    ),
    "health_notification_repeat_hours": ConfigKey(
        section="Health",
        default=24,
        value_type=PositiveInt,
        help="Minimum hours between emails for reopened instances of the same incident. "
        "Task stalls are grouped by task kind. A warning escalating to critical bypasses this delay.",
    ),
}
