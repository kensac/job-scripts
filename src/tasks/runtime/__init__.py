"""What a handler may call while it runs.

Sits below the handlers so a handler can import it without pulling in the
worker loop, while the loop imports handlers for its registry. That ordering
is what keeps the two from forming a cycle.

It was one 775-line module doing four jobs at once. It is now three, by job,
and the fourth left the package: `configured_model` is `api.task_config` and
`load_config` is `api.budget`, both of which read the database on behalf of the
services rather than running anything.

- `limits` - how much runs at once, and how a sweep is cut into chunks.
- `lifecycle` - the claim, progress, the end of a task, and the parent a chunk
  reports to.
- `batching` - submitting a provider batch, parking on it, collecting it, and
  recording what it cost and what it asked.

The names are re-exported here because that is how they are used: a handler
wants a progress call, a chunk size and a batch submission in one import, and
which of the three modules it came from is not the handler's concern.

They are public because they are used that way. Eleven of them crossed a module
boundary while spelled with a leading underscore, so the underscore was a claim
about the interface that the callers falsified. What is still private is
private to one of the three modules and is reached through that module.
"""

from __future__ import annotations

from api.queue import enqueue
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
    HEARTBEAT_TIMEOUT_MINUTES,
    MAX_ATTEMPTS,
    Deferred,
    TaskClaim,
    cancelled,
    claim_guard,
    finish,
    maybe_finalize_parent,
    parent_cancelled,
    reconcile_chunks,
    set_current_claim,
    set_progress,
    update_parent_progress,
)
from tasks.runtime.limits import (
    BATCH_CHUNK_SIZE,
    CHUNK_SIZE,
    MAX_CONCURRENCY,
    SCRAPE_CONCURRENCY,
    AdaptiveLimiter,
)

__all__ = [
    "BATCH_CHUNK_SIZE",
    "CHUNK_KINDS",
    "CHUNK_SIZE",
    "HEARTBEAT_TIMEOUT_MINUTES",
    "MAX_ATTEMPTS",
    "MAX_CONCURRENCY",
    "SCRAPE_CONCURRENCY",
    "AdaptiveLimiter",
    "AwaitingBatch",
    "BatchProvenance",
    "Deferred",
    "TaskClaim",
    "batch_event_hook",
    "cancelled",
    "claim_guard",
    "collect_pending",
    "consume_result",
    "enqueue",
    "finish",
    "has_batch_work",
    "maybe_finalize_parent",
    "parent_cancelled",
    "park_awaiting_batch",
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
