"""Current provider progress and terminal-state interpretation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)
TERMINAL_STATES = {"completed", "failed", "expired", "cancelled"}


@dataclass
class BatchProgress:
    """What the provider says about a batch right now.

    The counts ride along with the status because the retrieve call already
    returns them. Fetching a status and discarding the progress is what left
    ai_batches.completed at 0 for the whole life of every batch, so the only
    way to answer "is this moving or stuck" was to curl the provider by hand.
    """

    status: str
    total: int = 0
    completed: int = 0
    failed: int = 0


async def read_batch_progress(
    client: AsyncOpenAI, batch_ids: list[str]
) -> dict[str, BatchProgress]:
    """Provider status and counts per batch id. A status call only - it never
    downloads output - so polling many batches costs about as much as one."""
    out: dict[str, BatchProgress] = {}
    for batch_id in batch_ids:
        try:
            batch = await client.batches.retrieve(batch_id)
            counts = getattr(batch, "request_counts", None)
            out[batch_id] = BatchProgress(
                status=batch.status,
                total=getattr(counts, "total", 0) or 0,
                completed=getattr(counts, "completed", 0) or 0,
                failed=getattr(counts, "failed", 0) or 0,
            )
        except Exception as exc:
            logger.warning(f"Batch {batch_id} status check failed: {exc}")
    return out


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES
