"""Keep answer_usage_hourly current (api.ledger_rollup).

Each step recomputes the hours writes have dirtied and sums up to two days more
of history (about 10 s on production), in its own transaction, until the last complete hour is summed.
The first run after the table appears sums all of history this way. Reads
are exact whether or not this has run; it only decides how much of a read is
already added up.
"""

from __future__ import annotations

import asyncio
from typing import Any

from api import ledger_rollup
from tasks.runtime import set_progress

_STEPS = 200


async def handle_ledger_rollup(task_id: int, payload: dict[str, Any]) -> None:
    dirty = steps = 0
    done: dict[str, Any] = {}
    while steps < _STEPS:
        done = await asyncio.to_thread(ledger_rollup.refresh)
        steps += 1
        dirty += done["dirty_hours"]
        if done["skipped"] or done["caught_up"]:
            break
    advanced = done.get("advanced_to")
    set_progress(
        task_id,
        steps,
        steps,
        f"{dirty} dirty hours recomputed; summed to {advanced:%Y-%m-%d %H:00}"
        if advanced
        else "another refresh is running",
    )
