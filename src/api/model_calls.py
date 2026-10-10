"""The ledger of paid model calls: one row per provider request.

`record` is the only writer of `model_calls`, and it prices each row, so a
call's cost is decided in one place at the time it ran. Batch items are
recorded by the receipt checkpoint, which every collected result passes
through; live calls by the live writers in `api.budget`. The design, and the
production measurements that say the sources agree, are in
docs/agents/architecture-migration.md ("The ledger of paid model calls").
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from api import db
from core import pricing
from core.batch import BatchResult


@dataclass(frozen=True)
class Payer:
    """Who a call is charged to. Neither id is the fleet."""

    user_id: int | None = None
    managed_board_id: int | None = None

    def __post_init__(self) -> None:
        if self.user_id is not None and self.managed_board_id is not None:
            raise ValueError("a call has one payer")

    @property
    def kind(self) -> str:
        if self.user_id is not None:
            return "user"
        return "fleet" if self.managed_board_id is None else "managed_board"

    @property
    def id(self) -> int | None:
        return self.user_id if self.user_id is not None else self.managed_board_id


FLEET = Payer()

_INSERT = (
    "INSERT INTO model_calls (purpose, model, user_id, managed_board_id, key_source, task_id, "
    "provider_batch_id, custom_id, prompt_tokens, completion_tokens, total_tokens, "
    "cached_tokens, cache_write_tokens, reasoning_tokens, cost_usd) VALUES "
    "(%(purpose)s, %(model)s, %(user_id)s, %(managed_board_id)s, %(key_source)s, %(task_id)s, "
    "%(provider_batch_id)s, %(custom_id)s, %(prompt_tokens)s, %(completion_tokens)s, "
    "%(total_tokens)s, %(cached_tokens)s, %(cache_write_tokens)s, %(reasoning_tokens)s, "
    "%(cost_usd)s) ON CONFLICT (provider_batch_id, custom_id) DO NOTHING"
)


@dataclass(frozen=True)
class Call:
    purpose: str
    model: str | None
    payer: Payer
    key_source: str
    # The normalised shape `ai.batch_usage` and `ai.parse` return.
    usage: Mapping[str, int | None]
    task_id: int | None = None
    provider_batch_id: str | None = None
    custom_id: str | None = None


def record(calls: Iterable[Call]) -> None:
    """Write each call once; a batch item already recorded is left alone.

    A call whose provider reported no tokens was not billed and is not a row.
    """
    rows: list[dict[str, Any]] = []
    for call in calls:
        usage = call.usage
        if not usage.get("total_tokens"):
            continue
        prompt = usage.get("prompt_tokens") or 0
        completion = usage.get("completion_tokens") or 0
        cached = usage.get("cached_tokens") or 0
        cache_write = usage.get("cache_write_tokens")
        rows.append(
            {
                "purpose": call.purpose,
                "model": call.model,
                "user_id": call.payer.user_id,
                "managed_board_id": call.payer.managed_board_id,
                "key_source": call.key_source,
                "task_id": call.task_id,
                "provider_batch_id": call.provider_batch_id,
                "custom_id": call.custom_id,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": usage.get("total_tokens"),
                "cached_tokens": cached,
                "cache_write_tokens": cache_write,
                "reasoning_tokens": usage.get("reasoning_tokens") or 0,
                "cost_usd": pricing.estimate_cost_usd(
                    call.model,
                    prompt,
                    completion,
                    cached_tokens=cached,
                    cache_write_tokens=cache_write,
                    batched=call.provider_batch_id is not None,
                ),
            }
        )
    if rows:
        db.executemany(_INSERT, rows)


def record_batch_items(results: Iterable[BatchResult]) -> None:
    """One row per collected batch item, from its receipt and its batch.

    Purpose, model and payer come from the batch's `ai_batches` row. A batch
    whose payer was never recorded (submitted before payers were) writes
    nothing here: its calls are the backfill's, not a guess at who paid.
    """
    from api.ai import batch_usage

    paid = [result for result in results if result.batch_id and result.usage]
    if not paid:
        return
    batches = {
        row["provider_batch_id"]: row
        for row in db.query(
            "SELECT provider_batch_id, task_id, purpose, model, payer, payer_id FROM ai_batches "
            "WHERE provider_batch_id = ANY(%s) AND payer IS NOT NULL",
            (list({result.batch_id for result in paid}),),
        )
    }
    calls = []
    for result in paid:
        batch = batches.get(result.batch_id)
        if batch is None:
            continue
        payer = Payer(
            user_id=batch["payer_id"] if batch["payer"] == "user" else None,
            managed_board_id=batch["payer_id"] if batch["payer"] == "managed_board" else None,
        )
        calls.append(
            Call(
                purpose=batch["purpose"],
                model=batch["model"],
                payer=payer,
                # Batches run on the server's key. A person's or a board's
                # share of it is 'owner', as api_usage books it.
                key_source="server" if payer == FLEET else "owner",
                usage=batch_usage(result.usage),
                task_id=batch["task_id"],
                provider_batch_id=result.batch_id,
                custom_id=result.custom_id,
            )
        )
    record(calls)
