"""Persisted fleet settings and their admin metadata."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, NonNegativeInt, PositiveInt

from api.config_types import ConfigKey
from core.fallback import FallbackChains

CONFIG_KEYS: dict[str, ConfigKey] = {
    # The site-wide AI switch (api.ai_pause), for a stretch nobody uses the
    # application: ingest and page fetches keep the catalog current, and no
    # model is paid.
    "ai_paused": ConfigKey(
        section="Fleet",
        default=False,
        value_type=bool,
        help="Stops every paid model request: verification, filters, boards, extraction, "
        "embeddings and the AI buttons. Ingest and page fetches keep running. Batches already "
        "submitted are still collected.",
    ),
    "ai_resume_backlog_days": ConfigKey(
        section="Fleet",
        default=7,
        value_type=PositiveInt,
        help="When AI is switched back on, postings first seen during the pause are read only "
        "from this many days before it ended; older ones stay in the catalog unread.",
    ),
    # Read by api.model_fallback. Empty: a provider out of credit fails the
    # work, as it did on 2026-10-31, and nothing moves to another model.
    "model_fallback_chains": ConfigKey(
        section="Fleet",
        default=FallbackChains().model_dump(mode="json"),
        value_type=FallbackChains,
        kind="json",
        help="Per task purpose, the models to try in order when the running model's provider "
        "reports the account cannot pay (out of credit, billing hard limit, quota used up), e.g. "
        '{"verify": ["claude-haiku-5-5"]}. Rate limits and outages never move work. An entry '
        "that cannot run the task (no batch endpoint, structured output or effort it needs) is "
        "skipped, and the provider is tried again one scheduler cycle after its last refusal. "
        "Entries in the primary's verdict_model_equivalence group are tried first. Every answer "
        "is recorded under the model that gave it: an entry outside that group writes verdicts "
        "the primary's readers do not count, so those postings are asked again once the primary "
        "is back. Spend stays inside the same budgets and the fleet ceiling. Managed boards never "
        "fall back: a board reads only its own model's verdicts.",
    ),
    # The scheduler buckets the hour by this, so it must divide 60 for every
    # bucket to be the same length.
    "ingest_interval_minutes": ConfigKey(
        section="Fleet",
        default=60,
        value_type=Literal[1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60],
        help="Minutes between scheduler cycles: board pulls, mail sync and every hourly sweep. "
        "Divides 60. The queue backlog alert counts in these cycles.",
    ),
    # A week's fleet ceiling as a multiple of one sweep of every task at its
    # current model, so it moves with the design instead of needing a new
    # dollar figure each time a task or model changes. One sweep was $11.71
    # and the busiest week spent $29.19, about 2.5 sweeps, because sweeps
    # mostly find nothing to do. 24 leaves about ten times that headroom; a
    # single task switched to the dearest model it can reach costs $30 an
    # hour and breaches it inside a day.
    "fleet_weekly_cycles": ConfigKey(
        section="Fleet",
        default=24,
        value_type=NonNegativeInt,
        help="Weekly fleet spend ceiling, in full sweeps of every task at its current model. "
        "Batched submissions stop once the week passes it. 0 disables the check, which is "
        "how a deliberate backfill runs.",
    ),
    # The shared queue load-balances by availability, so fast workers claim
    # more chunks; the size bounds how much work one lost worker takes with it.
    "filter_chunk_size": ConfigKey(
        section="Fleet",
        default=100,
        value_type=PositiveInt,
        help="Checks per chunk when a live filter run or a re-verification sweep is split "
        "across the fleet.",
    ),
    "filter_batch_chunk_size": ConfigKey(
        section="Fleet",
        default=500,
        value_type=PositiveInt,
        help="Postings per chunk when a scheduled filter run is split across the fleet. "
        "Each chunk submits its own half-price provider batch.",
    ),
    "task_max_attempts": ConfigKey(
        section="Fleet",
        default=3,
        value_type=PositiveInt,
        help="Claims a task gets before a lost worker or a transient error fails it for good.",
    ),
    # A worker heartbeats every api.worker.HEARTBEAT_SECONDS (60) from its own
    # thread whether or not the event loop is free, so a silent quarter hour is
    # a dead worker, not a slow task. Under two beats a single late beat would
    # requeue a live task under its worker.
    "task_heartbeat_timeout_minutes": ConfigKey(
        section="Fleet",
        default=15,
        value_type=Annotated[int, Field(ge=2)],
        help="Minutes without a heartbeat before the reaper requeues a running task.",
    ),
    # The switch for running several tasks on one worker (api.worker). Empty by
    # default: a worker not named runs one task at a time, the loop it ran
    # before this key existed, so a deploy changes nothing until a worker is
    # named here. The number is a ceiling; under it the worker's adaptive
    # limit finds its own level from throughput and free memory.
    "worker_task_slots": ConfigKey(
        section="Fleet",
        default={},
        value_type=dict[str, PositiveInt],
        kind="hosts",
        help="Worker name to the most task slots it fills at once. A worker whose host sets "
        "JOBTRACKER_TASK_SLOTS takes the smaller of the two, so an entry here can lower a host "
        "but not raise it. A worker with neither, or at 1, runs one task at a time. Under the "
        "ceiling the worker grows while its throughput improves and memory is free, and halves "
        "on memory pressure.",
    ),
    # What one task of a kind costs in slots. The kinds seeded at 2 can hold
    # SCRAPE_CONCURRENCY browsers at once (its default is 2; tasks.runtime.limits),
    # where an ingest holds at most one. A kind not listed costs one slot.
    "worker_task_weights": ConfigKey(
        section="Fleet",
        default={
            kind: 2
            for kind in (
                "fetch_missing_content",
                "reverify_open",
                "reverify_chunk",
                "run_filter",
                "run_filter_chunk",
                "run_filter_batch_chunk",
                "run_managed_board",
                "run_managed_board_batch",
            )
        },
        value_type=dict[str, PositiveInt],
        kind="hosts",
        help="Task kind to the slots one task of it takes on a worker running several at once. "
        "A kind not listed takes one slot.",
    ),
    # One headless browser on a posting page is the largest thing a task
    # starts: 413 to 490 MB physical footprint across its processes, measured
    # on two career pages on 2026-10-11 (macOS; Linux shares more and lands
    # lower). A worker with less free than this does not claim a second task,
    # and one already running several halves its limit.
    "worker_memory_reserve_mb": ConfigKey(
        section="Fleet",
        default=512,
        value_type=PositiveInt,
        help="Free memory, in MB, a worker running several tasks keeps in hand. It claims "
        "another task only with more than this free, and halves its limit when free memory "
        "falls under it.",
    ),
}
