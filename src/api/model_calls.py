"""The ledger of paid model calls: one row per provider request.

`record` is the only writer of `model_calls`, and it prices each row, so a
call's cost is decided in one place at the time it ran. Batch items are
recorded by the receipt checkpoint, which every collected result passes
through; live calls by the live writers in `api.budget`. The design, and the
production measurements that say the sources agree, are in
docs/agents/architecture-migration.md ("The ledger of paid model calls").
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
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
    "INSERT INTO model_calls (created_at, purpose, model, payer, user_id, managed_board_id, "
    "key_source, batched, task_id, provider_batch_id, custom_id, prompt_tokens, "
    "completion_tokens, total_tokens, cached_tokens, cache_write_tokens, reasoning_tokens, "
    "cost_usd, source) VALUES "
    "(COALESCE(%(created_at)s, now()), %(purpose)s, %(model)s, %(payer)s, %(user_id)s, "
    "%(managed_board_id)s, %(key_source)s, %(batched)s, %(task_id)s, %(provider_batch_id)s, "
    "%(custom_id)s, %(prompt_tokens)s, %(completion_tokens)s, %(total_tokens)s, "
    "%(cached_tokens)s, %(cache_write_tokens)s, %(reasoning_tokens)s, %(cost_usd)s, %(source)s) "
    "ON CONFLICT (provider_batch_id, custom_id) DO NOTHING"
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
    # Set only by the backfill: when the call was made, and what it was
    # copied from. A call recorded as it happens takes now() and 'call'.
    created_at: datetime.datetime | None = None
    source: str = "call"


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
                "created_at": call.created_at,
                "source": call.source,
                "purpose": call.purpose,
                "model": call.model,
                "payer": call.payer.kind,
                "batched": call.provider_batch_id is not None,
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


# --- Readers ------------------------------------------------------------------
#
# A row is one request except a backfilled batch that kept no per-request
# record, which stands for `requests` of them, so a count of calls is
# SUM(requests), never COUNT(*). key_source is NULL on calls whose record did
# not say whose key (live filter answers before 2026-09-13).


def user_spend_by_day(user_id: int) -> list[dict[str, Any]]:
    """A person's last 30 days, by day (the session's timezone) and key."""
    return db.query(
        """
        SELECT created_at::date AS day, key_source,
               SUM(total_tokens) AS tokens, SUM(requests) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(requests) FILTER (WHERE cost_usd IS NULL), 0) AS unpriced_calls
        FROM model_calls WHERE user_id = %s AND created_at > now() - interval '30 days'
        GROUP BY 1, 2 ORDER BY 1
        """,
        (user_id,),
    )


def user_spend_by_purpose(user_id: int) -> list[dict[str, Any]]:
    """A person's calls over all time, by purpose and model."""
    return db.query(
        """
        SELECT purpose, model, SUM(total_tokens) AS tokens, SUM(requests) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COALESCE(SUM(requests) FILTER (WHERE cache_write_tokens IS NULL), 0)
                   AS cache_write_unknown_calls,
               COALESCE(SUM(requests) FILTER (WHERE cost_usd IS NULL), 0) AS unpriced_calls
        FROM model_calls WHERE user_id = %s GROUP BY 1, 2 ORDER BY 3 DESC
        """,
        (user_id,),
    )


# --- Backfill -----------------------------------------------------------------
#
# Copies the calls made before this table into it, each era from the record
# that was right in it (architecture-migration.md, "The ledger of paid model
# calls"). Every step is idempotent by predicate: a batch item by its unique
# (provider_batch_id, custom_id), a copied row by (source, source_id), a batch
# by its one aggregate row. A run cut short resumes; a second run adds nothing.
#
# Costs are copied as recorded, never repriced, except a receipt nothing else
# priced (step 2). Repricing every receipt at 2026-10-10's rates came to $7.32
# more than the batches recorded, so today's table is not history's.

# Purposes whose batches are only ever the fleet's: every caller of the batch
# hook for these passes no payer.
_FLEET_PURPOSES = (
    "verify",
    "reverify",
    "comp",
    "requirements",
    "locations",
    "job_profile",
    "mail_classify",
    "embedding",
    "experiment",
)

# A verdict names whose filter it answered in filter_name ('user12:name',
# 'managed-board:3'), written by the run that paid for it. A closed or
# clearance verdict is the fleet's sweep unless an admin asked for it by hand.
_VERDICT_PAYER = """
    LEFT JOIN users pu ON pu.id = substring(q.filter_name FROM '^user([0-9]+):')::bigint
    LEFT JOIN managed_boards pb
        ON pb.id = substring(q.filter_name FROM '^managed-board:([0-9]+)$')::bigint
"""
_VERDICT_PAYER_KIND = """
    CASE WHEN pu.id IS NOT NULL THEN 'user'
         WHEN pb.id IS NOT NULL THEN 'managed_board'
         WHEN q.check_type IN ('closed', 'clearance')
              AND q.config_name IS DISTINCT FROM 'manual' THEN 'fleet'
    END
"""

_COLUMNS = (
    "created_at, purpose, model, payer, user_id, managed_board_id, key_source, batched, "
    "task_id, provider_batch_id, custom_id, requests, prompt_tokens, completion_tokens, "
    "total_tokens, cached_tokens, cache_write_tokens, reasoning_tokens, cost_usd, source, "
    "source_id"
)

# Step 1: a batched verdict that carries tokens is exactly one batch item (no
# (batch_id, url) held two on 2026-10-10), and its cost was priced when the
# answer came back. Its siblings from the same answer carry zeros and are not
# calls. A verdict's url is its request's custom_id.
#
# A verify batch is the fleet's even where its paid row is a board's custom
# answer (9 rows on 2026-10-10: the closed and clearance answers were already
# settled, so the board's question came first). The label says whose
# question, the purpose says who paid.
_FROM_BATCHED_VERDICTS = f"""
    INSERT INTO model_calls ({_COLUMNS})
    SELECT q.created_at, b.purpose, q.model, p.kind,
           CASE WHEN p.kind = 'user' THEN pu.id END,
           CASE WHEN p.kind = 'managed_board' THEN pb.id END,
           CASE WHEN p.kind = 'fleet' THEN 'server' WHEN p.kind IS NOT NULL THEN 'owner' END,
           true, b.task_id, q.batch_id, q.url, 1,
           q.prompt_tokens, COALESCE(q.completion_tokens, 0), q.total_tokens,
           COALESCE(q.cached_tokens, 0), q.cache_write_tokens, q.reasoning_tokens, q.cost_usd,
           'verdict', q.id
    FROM ai_queries q
    JOIN ai_batches b ON b.provider_batch_id = q.batch_id
    {_VERDICT_PAYER}
    CROSS JOIN LATERAL (
        SELECT CASE WHEN b.purpose = ANY(%(fleet)s) THEN 'fleet'
                    ELSE {_VERDICT_PAYER_KIND} END AS kind
    ) p
    WHERE q.id > %(after)s AND q.id <= %(upto)s
      AND q.batch_id IS NOT NULL AND q.total_tokens > 0
    ON CONFLICT DO NOTHING
"""

# Step 4: a live verdict call from before the ledger recorded live calls.
# explain is left to api_usage, which recorded it with its payer.
_FROM_LIVE_VERDICTS = f"""
    INSERT INTO model_calls ({_COLUMNS})
    SELECT q.created_at,
           CASE WHEN q.check_type = 'custom' AND pb.id IS NOT NULL THEN 'managed_board'
                WHEN q.check_type = 'custom' THEN 'filter'
                WHEN q.config_name = 'manual' THEN 'manual'
                WHEN q.config_name = 'reverify' THEN 'reverify'
                ELSE 'verify' END,
           q.model, {_VERDICT_PAYER_KIND}, pu.id, pb.id, NULL, false, NULL, NULL, NULL, 1,
           q.prompt_tokens, COALESCE(q.completion_tokens, 0), q.total_tokens,
           COALESCE(q.cached_tokens, 0), q.cache_write_tokens, q.reasoning_tokens, q.cost_usd,
           'verdict', q.id
    FROM ai_queries q
    {_VERDICT_PAYER}
    WHERE q.id > %(after)s AND q.id <= %(upto)s
      AND q.batch_id IS NULL AND q.total_tokens > 0
      AND q.config_name IS DISTINCT FROM 'explain'
      AND q.created_at < %(cutover)s
    ON CONFLICT DO NOTHING
"""

# Step 3: the part of a batch that no item accounts for, as one row: a whole
# batch that kept no receipts and wrote no verdict (mail, requirements and
# others before 2026-09-09), or the calls of an older batch that wrote no
# verdict. Tokens and cost are the batch's recorded totals less its items'.
# Experiments are api_usage's (step 5), which recorded them per result.
_BATCH_REMAINDERS = f"""
    INSERT INTO model_calls ({_COLUMNS})
    SELECT COALESCE(b.completed_at, b.updated_at), b.purpose, b.model, p.kind, p.user_id,
           p.board_id, CASE WHEN p.kind = 'fleet' THEN 'server'
                            WHEN p.kind IS NOT NULL THEN 'owner' END,
           true, b.task_id, b.provider_batch_id, NULL,
           GREATEST(b.completed - COALESCE(i.n, 0), 1),
           b.input_tokens - COALESCE(i.prompt, 0), b.output_tokens - COALESCE(i.completion, 0),
           b.input_tokens + b.output_tokens - COALESCE(i.total, 0),
           0, NULL, NULL, b.est_cost_usd - COALESCE(i.cost, 0), 'batch', b.id
    FROM ai_batches b
    LEFT JOIN LATERAL (
        SELECT count(*) AS n, sum(prompt_tokens) AS prompt, sum(completion_tokens) AS completion,
               sum(total_tokens) AS total, sum(cost_usd) AS cost
        FROM model_calls m WHERE m.provider_batch_id = b.provider_batch_id
    ) i ON true
    LEFT JOIN tasks t ON t.id = b.task_id
    LEFT JOIN users tu ON tu.id = (t.payload->>'user_id')::bigint
    LEFT JOIN managed_boards tb ON tb.id = (t.payload->>'managed_board_id')::bigint
    CROSS JOIN LATERAL (
        SELECT CASE WHEN b.payer IS NOT NULL THEN b.payer
                    WHEN tu.id IS NOT NULL THEN 'user'
                    WHEN tb.id IS NOT NULL THEN 'managed_board'
                    WHEN b.purpose = ANY(%(fleet)s) THEN 'fleet' END AS kind,
               CASE WHEN b.payer = 'user' THEN b.payer_id
                    WHEN b.payer IS NULL THEN tu.id END AS user_id,
               CASE WHEN b.payer = 'managed_board' THEN b.payer_id
                    WHEN b.payer IS NULL AND tu.id IS NULL THEN tb.id END AS board_id
    ) p
    WHERE b.purpose <> 'experiment'
      AND b.input_tokens + b.output_tokens > COALESCE(i.total, 0)
      -- A batch's totals land a moment before its receipts do, in the same
      -- collection; a day later a batch with no receipts never had any.
      AND COALESCE(b.completed_at, b.updated_at) < now() - interval '1 day'
      AND NOT EXISTS (SELECT 1 FROM batch_result_receipts r
                      WHERE r.provider_batch_id = b.provider_batch_id)
    ON CONFLICT DO NOTHING
"""

# Step 5: calls only api_usage recorded, with the payer it recorded: live
# drafts, extraction, prompt help, explanations, and the experiment results
# of 2026-09-07. Filter and board work is not taken from here: before
# 2026-09-13 it booked batched requests at live prices, and the verdicts and
# receipts hold the same calls priced right. Batched drafts are receipts.
_FROM_USAGE = f"""
    INSERT INTO model_calls ({_COLUMNS})
    SELECT u.created_at, u.purpose, u.model,
           CASE WHEN u.user_id IS NOT NULL THEN 'user'
                WHEN u.managed_board_id IS NOT NULL THEN 'managed_board' ELSE 'fleet' END,
           u.user_id, u.managed_board_id, u.key_source, u.batched, NULL, NULL, NULL, 1,
           u.prompt_tokens, u.completion_tokens, u.total_tokens, u.cached_tokens,
           u.cache_write_tokens, NULL, u.cost_usd, 'usage', u.id
    FROM api_usage u
    WHERE u.purpose IN ('application', 'extract', 'improve_prompt', 'explain', 'experiment')
      AND NOT (u.purpose = 'application' AND u.batched)
      AND u.created_at < %(cutover)s
    ON CONFLICT DO NOTHING
"""

# Ids per statement for the ai_queries steps: about 1.9 million answers on
# 2026-10-10, so about forty statements, each a short range of the primary key.
_ID_SPAN = 50_000

_MICRO = Decimal("0.000001")


def backfill_cutover() -> datetime.datetime:
    """Live calls before this are the backfill's, after it the writer's.

    The first live call this table recorded. Until there is one, now: every
    live call so far is in the old records and none is here.
    """
    row = db.query_one(
        "SELECT COALESCE(min(created_at), now()) AS at FROM model_calls "
        "WHERE source = 'call' AND provider_batch_id IS NULL"
    )
    assert row is not None
    return row["at"]


def backfill_verdicts(live: bool, cutover: datetime.datetime) -> int:
    """Steps 1 (batched) and 4 (live), over ai_queries in id spans."""
    sql = _FROM_LIVE_VERDICTS if live else _FROM_BATCHED_VERDICTS
    bounds = db.query_one("SELECT COALESCE(max(id), 0) AS hi FROM ai_queries")
    assert bounds is not None
    written, after = 0, 0
    while after < bounds["hi"]:
        upto = after + _ID_SPAN
        written += db.execute_count(
            sql,
            {"after": after, "upto": upto, "cutover": cutover, "fleet": list(_FLEET_PURPOSES)},
        )
        after = upto
    return written


def backfill_receipts(limit: int = 200) -> int:
    """Step 2: receipt items no verdict accounted for, up to `limit` batches.

    An item that wrote no verdict (failed, superseded, invalid, or not yet
    consumed) was billed all the same, and its receipt is the only record of
    it, so it is priced from its tokens. A batch of a purpose that writes no
    verdict is priced that way only where it gives the cost the batch
    recorded; where it does not (174 batches on 2026-10-10, most of them job
    profiles, whose rates have changed since), the batch is one row at its
    recorded cost. Returns rows written; zero means nothing is left.
    """
    from api.ai import batch_usage

    batches = db.query(
        "SELECT b.provider_batch_id, b.purpose, b.model, b.task_id, b.est_cost_usd, b.payer, "
        "b.payer_id, t.payload->>'user_id' AS task_user, "
        "t.payload->>'managed_board_id' AS task_board, "
        "EXISTS (SELECT 1 FROM model_calls m WHERE m.provider_batch_id = b.provider_batch_id "
        "        AND m.source = 'verdict') AS has_verdicts "
        "FROM ai_batches b LEFT JOIN tasks t ON t.id = b.task_id "
        "WHERE EXISTS (SELECT 1 FROM batch_result_receipts r "
        "              WHERE r.provider_batch_id = b.provider_batch_id "
        "                AND (r.response->'usage'->>'total_tokens')::bigint > 0 "
        "                AND NOT EXISTS (SELECT 1 FROM model_calls m "
        "                                WHERE m.provider_batch_id = r.provider_batch_id "
        "                                  AND m.custom_id = r.custom_id)) "
        "  AND NOT EXISTS (SELECT 1 FROM model_calls m "
        "                  WHERE m.provider_batch_id = b.provider_batch_id "
        "                    AND m.custom_id IS NULL) "
        "ORDER BY b.id LIMIT %s",
        (limit,),
    )
    written = 0
    for batch in batches:
        payer = _batch_payer(batch)
        receipts = db.query(
            "SELECT r.custom_id, r.received_at, r.response->'usage' AS usage "
            "FROM batch_result_receipts r WHERE r.provider_batch_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM model_calls m WHERE m.provider_batch_id = "
            "r.provider_batch_id AND m.custom_id = r.custom_id)",
            (batch["provider_batch_id"],),
        )
        calls = [
            Call(
                purpose=batch["purpose"],
                model=batch["model"],
                payer=payer,
                key_source="server" if payer == FLEET else "owner",
                usage=batch_usage(r["usage"]),
                task_id=batch["task_id"],
                provider_batch_id=batch["provider_batch_id"],
                custom_id=r["custom_id"],
                created_at=r["received_at"],
                source="receipt",
            )
            for r in receipts
        ]
        calls = [call for call in calls if call.usage.get("total_tokens")]
        if not batch["has_verdicts"] and not _prices_as_recorded(batch, calls):
            written += _write_whole_batch(batch, payer, calls)
            continue
        record(calls)
        written += len(calls)
    return written


def _batch_payer(batch: Mapping[str, Any]) -> Payer:
    """The batch's recorded payer, else the payer its submitting task named."""
    if batch["payer"] == "user":
        return Payer(user_id=batch["payer_id"])
    if batch["payer"] == "managed_board":
        return Payer(managed_board_id=batch["payer_id"])
    if batch["payer"] is None and batch["task_user"] is not None:
        return Payer(user_id=int(batch["task_user"]))
    if batch["payer"] is None and batch["task_board"] is not None:
        return Payer(managed_board_id=int(batch["task_board"]))
    return FLEET


def _prices_as_recorded(batch: Mapping[str, Any], calls: list[Call]) -> bool:
    """Whether today's rates price these items to what the batch recorded,
    within a micro-dollar per item: each item rounds to the column's six
    places, and the batch rounded once."""
    recorded = batch["est_cost_usd"]
    costs = [_priced(call) for call in calls]
    if recorded is None or any(cost is None for cost in costs):
        return recorded is None and all(cost is None for cost in costs)
    total = sum((cost.quantize(_MICRO) for cost in costs if cost is not None), Decimal(0))
    return abs(total - recorded) <= _MICRO * max(len(calls), 1)


def _priced(call: Call) -> Decimal | None:
    usage = call.usage
    return pricing.estimate_cost_usd(
        call.model,
        usage.get("prompt_tokens") or 0,
        usage.get("completion_tokens") or 0,
        cached_tokens=usage.get("cached_tokens") or 0,
        cache_write_tokens=usage.get("cache_write_tokens"),
        batched=True,
    )


def _write_whole_batch(batch: Mapping[str, Any], payer: Payer, calls: list[Call]) -> int:
    if not calls:
        return 0
    usages = [call.usage for call in calls]
    writes = [u.get("cache_write_tokens") for u in usages]
    return db.execute_count(
        f"INSERT INTO model_calls ({_COLUMNS}) "
        "SELECT COALESCE(b.completed_at, b.updated_at), b.purpose, b.model, %(payer)s, "
        "%(user_id)s, %(board_id)s, %(key_source)s, true, b.task_id, b.provider_batch_id, "
        "NULL, %(requests)s, %(prompt)s, %(completion)s, %(total)s, %(cached)s, "
        "%(cache_write)s, %(reasoning)s, b.est_cost_usd, 'batch', b.id "
        "FROM ai_batches b WHERE b.provider_batch_id = %(bid)s ON CONFLICT DO NOTHING",
        {
            "bid": batch["provider_batch_id"],
            "payer": payer.kind,
            "user_id": payer.user_id,
            "board_id": payer.managed_board_id,
            "key_source": "server" if payer == FLEET else "owner",
            "requests": len(calls),
            "prompt": sum(u.get("prompt_tokens") or 0 for u in usages),
            "completion": sum(u.get("completion_tokens") or 0 for u in usages),
            "total": sum(u.get("total_tokens") or 0 for u in usages),
            "cached": sum(u.get("cached_tokens") or 0 for u in usages),
            "cache_write": None if None in writes else sum(w or 0 for w in writes),
            "reasoning": sum(u.get("reasoning_tokens") or 0 for u in usages),
        },
    )


def backfill_remainders() -> int:
    """Step 3. One statement: about 10,000 batches on 2026-10-10."""
    return db.execute_count(_BATCH_REMAINDERS, {"fleet": list(_FLEET_PURPOSES)})


def backfill_usage(cutover: datetime.datetime) -> int:
    """Step 5. One statement: about 5,000 rows on 2026-10-10."""
    return db.execute_count(_FROM_USAGE, {"cutover": cutover})
