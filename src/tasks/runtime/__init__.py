"""Runtime operations shared by task handlers.

Handlers import this package without importing the worker loop. The worker
imports handlers for its registry, so this dependency order avoids a cycle.

`limits` controls concurrency and chunk sizes. `lifecycle` handles claims,
progress and completion. `batching` submits and collects provider batches.
The public operations are re-exported here for handler imports.

Database-backed configuration lives in `api.task_config` and `api.budget`.
"""

from __future__ import annotations

from api.queue import enqueue, merge_payload
from tasks.runtime.batching import (
    AwaitingBatch,
    BatchProvenance,
    batch_event_hook,
    collect_pending,
    consume_result,
    has_batch_work,
    park_awaiting_batch,
    pending_batch_ids,
    repark_if_unfinished,
    resume_parked,
    run_batched,
    snapshot_specs,
    submit_or_collect,
)
from tasks.runtime.lifecycle import (
    CHUNK_KINDS,
    Deferred,
    TaskClaim,
    cancelled,
    checkpoint,
    claim_guard,
    fail_unavailable_payload,
    finish,
    maybe_finalize_parent,
    parent_cancelled,
    park_waiting,
    reconcile_chunks,
    set_current_claim,
    set_progress,
    update_parent_progress,
)
from tasks.runtime.limits import (
    MAX_CONCURRENCY,
    SCRAPE_CONCURRENCY,
    AdaptiveLimiter,
    available_memory_mb,
)

__all__ = [
    "CHUNK_KINDS",
    "MAX_CONCURRENCY",
    "SCRAPE_CONCURRENCY",
    "AdaptiveLimiter",
    "AwaitingBatch",
    "BatchProvenance",
    "Deferred",
    "TaskClaim",
    "available_memory_mb",
    "batch_event_hook",
    "cancelled",
    "checkpoint",
    "claim_guard",
    "collect_pending",
    "consume_result",
    "enqueue",
    "fail_unavailable_payload",
    "finish",
    "has_batch_work",
    "maybe_finalize_parent",
    "merge_payload",
    "parent_cancelled",
    "park_awaiting_batch",
    "park_waiting",
    "pending_batch_ids",
    "reconcile_chunks",
    "repark_if_unfinished",
    "resume_parked",
    "run_batched",
    "set_current_claim",
    "set_progress",
    "snapshot_specs",
    "submit_or_collect",
    "update_parent_progress",
]
