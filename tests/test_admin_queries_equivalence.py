"""GET /admin/queries and its option lists answer exactly what the SQL they
replaced answered. The list lost NULLS LAST on its NOT NULL sort keys and the
options became skip scans; both were speed changes only, so the old
statements are kept here verbatim as the reference and every response is
compared against them on rows built to hit the edges: NULLs and empty
strings in every option column, ties in every sort key, values seen only
outside the 30-day window, and an empty table."""

from __future__ import annotations

import pytest

from api import db, sorting
from api.routers.admin import queries

_OLD_OPTIONS = {
    "check_types": "SELECT DISTINCT check_type AS v FROM ai_queries "
    "WHERE check_type IS NOT NULL ORDER BY check_type",
    "statuses": "SELECT DISTINCT status AS v FROM ai_queries WHERE status IS NOT NULL ORDER BY status",
    "contexts": "SELECT DISTINCT config_name AS v FROM ai_queries WHERE config_name IS NOT NULL "
    "AND created_at > now() - interval '30 days' ORDER BY config_name",
    "workers": "SELECT DISTINCT worker AS v FROM ai_queries WHERE worker IS NOT NULL "
    "AND created_at > now() - interval '30 days' ORDER BY worker",
}
_OLD_VOCABULARY = {
    "check_types": "SELECT DISTINCT check_type AS v FROM ai_queries "
    "WHERE check_type IS NOT NULL ORDER BY check_type",
    "statuses": "SELECT DISTINCT status AS v FROM ai_queries WHERE status IS NOT NULL ORDER BY status",
    "configs": "SELECT DISTINCT config_name AS v FROM ai_queries "
    "WHERE config_name IS NOT NULL ORDER BY config_name",
}


def _fill() -> None:
    # 48 rows: every option column cycles through NULL, an empty string, and
    # values whose collation order differs from byte order ('B' vs 'a'); half
    # the rows are 40 days old, and 'legacy'/'oldbox' exist only there.
    # created_at repeats in pairs and the numeric keys repeat or are NULL, so
    # every sort key has ties for the id tiebreaker to settle.
    db.execute(
        """
        INSERT INTO ai_queries (created_at, url, check_type, status, config_name, worker,
                                company, total_tokens, duration_ms)
        SELECT now() - CASE WHEN i % 2 = 0 THEN interval '40 days' ELSE interval '1 day' END
                     - (i / 2) * interval '1 minute',
               'https://eq.test/' || i,
               (ARRAY[NULL, '', 'closed', 'Custom', 'content'])[1 + i % 5],
               (ARRAY[NULL, 'passed', '', 'Rejected'])[1 + i % 4],
               CASE WHEN i % 2 = 0 THEN (ARRAY[NULL, 'legacy', 'B-batch', ''])[1 + (i / 2) % 4]
                    ELSE (ARRAY[NULL, 'verify-batch', '', 'a-cache', 'B-batch'])[1 + i % 5] END,
               CASE WHEN i % 2 = 0 THEN (ARRAY[NULL, 'oldbox', 'Pi'])[1 + i % 3]
                    ELSE (ARRAY[NULL, 'nas', '', 'Pi'])[1 + i % 4] END,
               (ARRAY[NULL, 'Acme', 'acme', 'Zed'])[1 + i % 4],
               (ARRAY[NULL, 10, 10, 7])[1 + i % 4],
               CASE WHEN i % 3 = 0 THEN NULL ELSE i % 5 END
        FROM generate_series(1, 48) i
        """
    )


def _old(sql: str) -> list:
    return [r["v"] for r in db.query(sql)]


@pytest.mark.parametrize("filled", [True, False], ids=["rows", "empty"])
def test_options_match_the_distinct_queries_they_replaced(client, admin_headers, filled):
    if filled:
        _fill()
    body = client.get("/v1/admin/queries/options", headers=admin_headers).json()
    for key, sql in _OLD_OPTIONS.items():
        # The route always dropped empty strings from these lists.
        assert body[key] == [v for v in _old(sql) if v], key
    vocabulary = client.get("/v1/admin/options", headers=admin_headers).json()
    for key, sql in _OLD_VOCABULARY.items():
        # This one never did: '' is a value it has always listed.
        assert vocabulary[key] == _old(sql), key
    if filled:
        # The fixture reaches what it claims to: a 40-day-only value, an
        # empty string, and a collation order that is not byte order.
        assert "legacy" in vocabulary["configs"] and "legacy" not in body["contexts"]
        assert "" in vocabulary["check_types"]
        assert body["contexts"] == ["a-cache", "B-batch", "verify-batch"]


@pytest.mark.parametrize("filled", [True, False], ids=["rows", "empty"])
def test_list_orders_exactly_as_with_nulls_last_everywhere(client, admin_headers, filled):
    if filled:
        _fill()
    for key in sorted(queries._SORTABLE):
        for direction in ("asc", "desc"):
            expected = [
                r["id"]
                for r in db.query(
                    f"SELECT id FROM ai_queries ORDER BY {key} {direction.upper()} NULLS LAST, "
                    "id DESC"
                )
            ]
            got: list[int] = []
            for page in (1, 2, 3, 4):
                body = client.get(
                    "/v1/admin/queries",
                    params={"sort": key, "dir": direction, "page": page, "page_size": 15},
                    headers=admin_headers,
                ).json()
                got += [r["id"] for r in body["rows"]]
            assert got == expected, (key, direction)


def test_not_null_keys_lose_only_their_nulls_clause():
    # Derived from the model: id and created_at are NOT NULL, nothing else
    # sortable on ai_queries is.
    assert {"id", "created_at"} == queries._NOT_NULL
    table = {"id": "id", "created_at": "created_at", "total_tokens": "total_tokens"}
    sorts = sorting.parse("id,created_at,total_tokens", "desc,asc,desc", table, "id")
    assert sorting.clause(sorts, table, queries._NOT_NULL) == (
        "id DESC, created_at ASC, total_tokens DESC NULLS LAST"
    )
    # Every other caller passes nothing and keeps its exact clause.
    assert sorting.clause(sorts, table) == (
        "id DESC NULLS LAST, created_at ASC NULLS LAST, total_tokens DESC NULLS LAST"
    )
