"""api.ledger_rollup reads are exact: whatever the refresh has or has not
done, and after writes that land in hours already summed, a read of a window
equals aggregating the source tables over it."""

from __future__ import annotations

import datetime
from decimal import Decimal

from api import db, ledger_rollup

_KEY = ("hour", *ledger_rollup.DIMENSIONS)
_DIRECT_ALL = "(SELECT '-infinity'::timestamptz AS lo, 'infinity'::timestamptz AS hi)"


def _call(cost: str | None, tokens: int, write: int | None = 1) -> int:
    row = db.query_one(
        "INSERT INTO model_calls (purpose, batched, prompt_tokens, completion_tokens, "
        "total_tokens, cached_tokens, cache_write_tokens, reasoning_tokens, cost_usd) "
        "VALUES ('verify', false, %s, 1, %s, 0, %s, 0, %s) RETURNING id",
        (tokens, tokens + 1, write, Decimal(cost) if cost else None),
    )
    assert row
    return row["id"]


def _answer(at: str, call: int | None, **cols: object) -> int:
    values = {
        "url": "https://r.test/1",
        "check_type": "closed",
        "status": "passed",
        "model": "m1",
        "config_name": "verify-batch",
        "prompt_hash": None,
        "batch_id": None,
    } | cols
    names = ", ".join(values)
    row = db.query_one(
        f"INSERT INTO ai_queries (created_at, model_call_id, {names}) "
        f"VALUES (%s, %s, {', '.join(['%s'] * len(values))}) RETURNING id",
        (at, call, *values.values()),
    )
    assert row
    return row["id"]


def _fetch(at: str, status: str = "passed", method: str = "scraped") -> None:
    db.execute(
        "INSERT INTO page_fetches (url, status, method, created_at) VALUES (%s, %s, %s, %s)",
        ("https://r.test/f", status, method, at),
    )


def _rows(sql: str, params: dict) -> list[dict]:
    rows = db.query(sql, params)
    return sorted(rows, key=lambda r: tuple(str(r[k]) for k in _KEY))


def _read(start: str = "-infinity", end: str = "infinity") -> list[dict]:
    groups = ledger_rollup.groups("%(start)s", "%(end)s")
    return _rows(f"SELECT * FROM {groups} g", {"start": start, "end": end})


def _direct(start: str = "-infinity", end: str = "infinity") -> list[dict]:
    ranges = "(SELECT %(start)s::timestamptz AS lo, %(end)s::timestamptz AS hi)"
    return _rows(
        f"SELECT * FROM ({ledger_rollup.aggregate(ranges)}) g", {"start": start, "end": end}
    )


def _until_caught_up() -> None:
    for _ in range(50):
        if ledger_rollup.refresh(max_span=datetime.timedelta(hours=7))["caught_up"]:
            return
    raise AssertionError("refresh never caught up")


def test_reads_equal_the_source_before_and_after_refresh_and_late_writes():
    a = _call("0.002", 100)
    b = _call(None, 50, write=None)
    c = _call("0.004", 70)
    first = _answer("2026-10-01 10:05+00", a)
    _answer("2026-10-01 10:05+00", a, check_type="clearance")  # sibling: zeros
    _answer("2026-10-01 11:59+00", b, status="failed", batch_id="batch-1")
    _answer("2026-10-01 23:30+00", None, model=None, config_name="manual")
    _answer("2026-10-02 00:10+00", c, prompt_hash="h1", check_type="custom")
    _fetch("2026-10-01 10:30+00")
    _fetch("2026-10-01 10:31+00", method="verification")  # not in the ledger
    _fetch("2026-10-02 03:00+00", status="failed")

    assert _read() == _direct()
    _until_caught_up()
    assert db.query_one("SELECT count(*) AS n FROM answer_usage_hourly")["n"] > 0
    assert _read() == _direct()

    # Writes into hours already summed: a repriced call, a late answer, an
    # earlier answer linked to a call (it becomes the call's first), a
    # status change and a deleted fetch.
    db.execute("UPDATE model_calls SET cost_usd = 0.009 WHERE id = %s", (c,))
    _answer("2026-10-01 12:00+00", a, status="rejected")
    early = _answer("2026-10-01 09:00+00", None)
    db.execute("UPDATE ai_queries SET model_call_id = %s WHERE id = %s", (a, early))
    db.execute("UPDATE ai_queries SET status = 'rejected' WHERE id = %s", (first,))
    db.execute("DELETE FROM page_fetches WHERE status = 'failed'")
    assert db.query_one("SELECT count(*) AS n FROM ledger_dirty_hours")["n"] > 0
    assert _read() == _direct()

    _until_caught_up()
    assert db.query_one("SELECT count(*) AS n FROM ledger_dirty_hours")["n"] == 0
    assert _read() == _direct()
    # A window that starts and ends inside hours reads only its part of them.
    window = ("2026-10-01 10:20+00", "2026-10-02 00:30+00")
    assert _read(*window) == _direct(*window)
    assert _read(*window) != _read()
