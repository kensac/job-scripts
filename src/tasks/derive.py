"""Derived facts: one registration each, one sweep for all of them.

A derived fact is a model's answer about something we already hold (a page we
fetched, a location string), stored so nothing pays for it twice. Pay,
requirements, job profiles, embeddings and locations each had their own sweep
repeating one skeleton: pick candidates from the current page text, skip the
ones whose text did not change, submit a batch, collect it, check the page is
still the one that was asked about, parse, store. Five copies of that skeleton
had drifted in what they called a failure, whether they rechecked their switch
at handoff, and whether two runs could overlap.

A `Derivation` declares what differs: its table, its input (page text cut to
`input_chars`, or something else when that is None), its recipe version, its
model routing, its staleness rule (`select`, which returns only stale or
missing answers) and its store. `sweep` is the skeleton, written once.
Adding a derivation is a registration in `tasks.DERIVATIONS`; the worker
schedules and dispatches every registered one.

A switched-off derivation is still registered. Its `switch` names the
app_config row, and off means no new submission: the scheduler does not
enqueue it, and a run started by hand stops before selecting and again at
handoff. Paid batches already submitted are still collected, because
collection is reachable whatever the switch says.

Not every derived value is registered. `jobs.near_copy_key` (core.near_copy)
is computed in process, costs nothing, and records the text verification
read, so the twin it finds was judged on the same text. A staleness rule
would recompute it from a newer page and break that. Measured 2026-10-10 on
3,000 sampled keyed postings: 2 keys differ from the current page's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from api import db
from api.ai import batch_results
from api.queue import enqueue
from api.task_admission import ACTIVE_STATUSES
from core.batch import BatchResult, BatchSpec
from core.routing import TaskShape
from tasks import rescrape
from tasks.runtime import (
    batch_event_hook,
    consume_result,
    has_batch_work,
    run_batched,
    set_progress,
    submit_or_collect,
)

logger = logging.getLogger(__name__)

Row = dict[str, Any]


@dataclass(frozen=True)
class Derivation:
    """One derived fact.

    `select(cap, payload)` is the staleness rule: it returns the candidates
    whose answer is missing or no longer matches its input, at most `cap`.
    A page-text derivation returns `url`, `content_row_id` and `input_content`
    per row, plus `stored_hash` when `skip_unchanged` is set.

    `requests(rows)` builds the batch specs. Their context is what collection
    reads back, so a change to it must still read a context an older image
    submitted.

    `store(result, context, answer)` writes one result and returns its receipt
    outcome; only "written" counts as done. `answer` is the parsed `answer`
    model, or None when `answer` is None and the store reads the raw result
    itself (embeddings pack many postings into one request).

    `recipe` is the version of the instructions and schema. A table that
    stores it (job_profiles.classifier_version) re-derives when it changes; a
    derivation whose table does not store it says None, and keeps its answers
    across a recipe change.

    Routing: `shape` goes through core.routing (and a person's configured
    model); `model` pins one. With no shape, `model` is submitted as is on the
    request's own endpoint, which is how embeddings run.
    """

    kind: str
    purpose: str
    noun: str
    table: str
    per_cycle_key: str
    select: Callable[[int, dict[str, Any]], list[Row]]
    requests: Callable[[list[Row]], list[BatchSpec]]
    store: Callable[[BatchResult, dict[str, Any], Any], str]
    input_chars: int | None
    recipe: str | None
    shape: TaskShape | None = None
    model: str | None = None
    answer: type[BaseModel] | None = None
    context_keys: Sequence[str] = ()
    skip_unchanged: bool = False
    switch: str | None = None
    has_work: Callable[[], bool] | None = None

    @property
    def reads_page(self) -> bool:
        return self.input_chars is not None

    async def handle(self, task_id: int, payload: dict[str, Any]) -> None:
        await sweep(task_id, self, payload)

    def schedule(self, cycle: str) -> None:
        schedule(self, cycle)


def _switched_off(d: Derivation) -> bool:
    return d.switch is not None and not db.get_config(d.switch)


def _active(kind: str, before: int | None = None) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT id FROM tasks WHERE kind = %s AND status = ANY(%s) "
        "AND (%s::bigint IS NULL OR id < %s) ORDER BY id LIMIT 1",
        (kind, list(ACTIVE_STATUSES), before, before),
    )


def schedule(d: Derivation, cycle: str) -> None:
    """Enqueue one pass when it is switched on, has work, and none is active.

    A pass is capped and then parks on the Batch API, which can take hours,
    so the cycle's dedupe key alone would stack a pass an hour on top of one
    still waiting. The active check stops overlap across cycles.
    """
    if _switched_off(d) or (d.has_work is not None and not d.has_work()) or _active(d.kind):
        return
    enqueue(d.kind, {"cycle": cycle}, dedupe_key=f"{d.kind}:{cycle}")


async def _submit(task_id: int, d: Derivation, specs: list[BatchSpec]) -> list[BatchResult]:
    if d.shape is None:
        assert d.model is not None, f"{d.kind} declares neither a shape nor a model"
        hook = batch_event_hook(task_id, d.purpose, d.model)
        return await submit_or_collect(task_id, specs, d.model, "", 0, hook)
    if d.model is None:
        results, _ = await run_batched(task_id, d.shape, specs)
        return results
    results, chosen = await run_batched(task_id, d.shape, specs, allow_configured_override=False)
    if chosen.model is not None and chosen.model != d.model:
        raise RuntimeError(f"{d.kind} resolved {chosen.model}, not its pinned {d.model}")
    return results


def _outcome(d: Derivation, result: BatchResult) -> str:
    context = result.request.context if result.request else None
    if not context or not all(context.get(key) for key in d.context_keys):
        return "unknown_request"
    if d.answer is None:
        return d.store(result, context, None)
    if d.reads_page and not rescrape.content_is_current(
        context.get("url") or result.custom_id, context["content_row_id"]
    ):
        return "superseded"
    if result.error or not result.text:
        return "failed"
    try:
        answer = d.answer.model_validate_json(result.text)
    except ValueError:
        logger.warning(f"{d.noun} parse failed for {result.custom_id}")
        return "invalid_output"
    return d.store(result, context, answer)


async def sweep(task_id: int, d: Derivation, payload: dict[str, Any]) -> None:
    specs: list[BatchSpec] = []
    if not has_batch_work(task_id):
        if _switched_off(d):
            set_progress(task_id, 0, 0, f"{d.noun} paused")
            return
        earlier = _active(d.kind, before=task_id)
        if earlier:
            # Two passes would select the same candidates and pay for them twice.
            set_progress(task_id, 0, 0, f"{d.kind} task {earlier['id']} is still in flight")
            return
        rows = d.select(int(db.get_config(d.per_cycle_key)), payload)
        if d.skip_unchanged:
            assert d.input_chars is not None
            rows = rescrape.drop_unchanged(rows, table=d.table, limit=d.input_chars)
        specs = d.requests(rows) if rows else []
        if not specs:
            set_progress(task_id, 0, 0, f"no {d.noun} to derive")
            return
        # Selection can outlive an admin changing the switch.
        if _switched_off(d):
            set_progress(task_id, 0, 0, f"{d.noun} paused")
            return
        set_progress(task_id, 0, len(specs), f"{d.noun} batch submitted")
    results = await _submit(task_id, d, specs)
    for n, result in enumerate(results, 1):
        with consume_result(task_id, result) as receipt:
            if receipt.pending:
                receipt.outcome = _outcome(d, result)
        if n % 200 == 0:
            set_progress(task_id, *batch_results.progress_counts(task_id), f"{d.noun} derived")
    set_progress(task_id, *batch_results.progress_counts(task_id), f"{d.noun} derived")
