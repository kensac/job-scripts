"""The company page and the job profile report were rewritten for speed and
must answer exactly as before. Each test keeps the statement it replaced,
verbatim, and compares the two over rows chosen to reach every branch: name
spellings that group together, a repost group whose display title is a
mode over several spellings, a group that loses on size, a page past the
end, and receipts without an output count."""

from __future__ import annotations

import datetime
import json

from api import db, signals
from api.routers import companies
from tests.test_api_jobs import _insert_job

_OLD_REPOST_SQL = """
WITH g AS (
    SELECT lower(btrim(company)) AS company_key,
           mode() WITHIN GROUP (ORDER BY title) AS title,
           count(*) AS url_count,
           min(date_posted) AS first_posted_at,
           max(date_posted) AS last_posted_at
    FROM jobs
    WHERE company <> '' AND title <> '' AND date_posted IS NOT NULL
      AND lower(btrim(company)) = ANY(%(keys)s)
    GROUP BY lower(btrim(company)), lower(btrim(title)), source, locations
    HAVING count(*) >= %(min_urls)s
       AND max(date_posted) - min(date_posted) > make_interval(days => %(min_span)s)
)
SELECT DISTINCT ON (company_key)
       company_key, title, url_count, first_posted_at, last_posted_at,
       (extract(epoch FROM last_posted_at - first_posted_at) / 86400)::int AS span_days
FROM g ORDER BY company_key, url_count DESC, last_posted_at DESC
"""

_OLD_OUTPUT_SQL = """
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY (r.response->'usage'->>'output_tokens')::int) AS output_tokens_p95,
  percentile_cont(0.99) WITHIN GROUP (ORDER BY (r.response->'usage'->>'output_tokens')::int) AS output_tokens_p99,
  max((r.response->'usage'->>'output_tokens')::int) AS output_tokens_max
FROM batch_result_receipts r JOIN tasks t ON t.id = r.task_id
WHERE t.kind = 'classify_job_profiles' AND r.response->'usage'->>'output_tokens' IS NOT NULL
"""

_DAY = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_SPAN = datetime.timedelta(days=signals.REPOST_MIN_SPAN_DAYS + 1)


def _catalog() -> None:
    n = 0

    def job(company: str, title: str, *, days: int = 0, source: str = "src-a", where="Austin"):
        nonlocal n
        n += 1
        _insert_job(
            source,
            f"https://eq.test/{n}",
            company=company,
            title=title,
            locations=[where],
            date_posted=_DAY + _SPAN * days,
        )

    # Repco: the winning group spells its title three ways (mode is "Data
    # Engineer", twice), a smaller qualifying group loses, and a same-title
    # group at another location stays separate.
    for i, title in enumerate(
        ["Data Engineer", "data engineer ", "Data Engineer", "DATA ENGINEER"]
    ):
        job("Repco", title, days=i)
    job("repco ", "Analyst", days=0)
    job("Repco", "Analyst", days=3)
    job("Repco", "Data Engineer", days=0, where="Boston")
    job("Repco", "Data Engineer", days=5, where="Boston")
    job("Repco", "Data Engineer", days=5, source="src-b")
    # Tightco reposts inside the span floor, so it has no repost at all.
    job("Tightco", "Clerk", days=0)
    job("Tightco", "Clerk", days=0)
    # Onceco has a single posting.
    job("Onceco", "Clerk", days=1)
    job("", "Nameless", days=1)


def _keys() -> list[str]:
    return [r["k"] for r in db.query("SELECT DISTINCT lower(btrim(company)) AS k FROM jobs")]


def test_repost_rewrite_answers_as_the_grouped_mode_did():
    _catalog()
    params = {
        "keys": _keys(),
        "min_urls": signals.REPOST_MIN_URLS,
        "min_span": signals.REPOST_MIN_SPAN_DAYS,
    }
    old = db.query(_OLD_REPOST_SQL, params)
    new = db.query(companies._REPOST_SQL, params)
    assert {r["company_key"] for r in old} == {"repco"}
    assert old[0]["title"] == "Data Engineer" and old[0]["url_count"] == 4
    assert sorted(new, key=lambda r: r["company_key"]) == old


def test_total_on_the_page_matches_the_separate_count(client, admin_headers):
    _catalog()
    for params in (
        {},
        {"limit": 2},
        {"limit": 2, "cursor": "2"},
        {"limit": 2, "cursor": "50"},  # past the end: no row carries the total
        {"repost": "true"},
        {"q": "nothing-matches"},
        {"q": "co", "sort": "company_name", "dir": "asc", "limit": 1, "cursor": "1"},
    ):
        body = client.get("/v1/admin/companies", params=params, headers=admin_headers).json()
        cut = {
            "q": f"%{params['q']}%" if "q" in params else None,
            "user_id": db.query_one(
                "SELECT id FROM users WHERE sub = %s", (admin_headers["X-User-Sub"],)
            )["id"],
            "applied": False,
            "has_comp": False,
            "repost": params.get("repost") == "true",
            "min_urls": signals.REPOST_MIN_URLS,
            "min_span": signals.REPOST_MIN_SPAN_DAYS,
        }
        count = db.query_one(companies._COUNT_SQL.format(cuts=companies._CUTS), cut)
        assert body["total_names"] == count["c"], params


def test_output_token_percentiles_match_the_whole_response_read(client, admin_headers):
    profiles = db.query_one(
        "INSERT INTO tasks (kind, payload) VALUES ('classify_job_profiles', '{}') RETURNING id"
    )["id"]
    other = db.query_one(
        "INSERT INTO tasks (kind, payload) VALUES ('filter_batch', '{}') RETURNING id"
    )["id"]
    usages = [{"output_tokens": n} for n in (120, 7, 900, 455, 455, 61)]
    usages += [{"input_tokens": 3}, {"output_tokens": None}, None]
    for i, usage in enumerate(usages):
        db.execute(
            "INSERT INTO batch_result_receipts (provider_batch_id, custom_id, task_id, response) "
            "VALUES ('b', %s, %s, %s)",
            (f"p{i}", profiles, json.dumps({"usage": usage, "finish_reason": "stop"})),
        )
    db.execute(
        "INSERT INTO batch_result_receipts (provider_batch_id, custom_id, task_id, response) "
        "VALUES ('b', 'o', %s, '{\"usage\": {\"output_tokens\": 99999}}')",
        (other,),
    )
    old = db.query_one(_OLD_OUTPUT_SQL)
    assert old["output_tokens_max"] == 900
    body = client.get("/v1/admin/job-profiles/report", headers=admin_headers).json()
    assert {k: body[k] for k in old} == old
