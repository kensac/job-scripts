"""sort=a,b&dir=x,y orders by a then b; unknown keys drop, a short dir
repeats, the echo says what was applied. The sheet sorts by several
columns at once, and the old single sort keeps working unchanged."""

from __future__ import annotations

import datetime

from api import sorting
from tests.test_api_jobs import _insert_job, _pass_closed, _subscribe, _uid


def test_parse_pairs_keys_with_dirs_and_drops_unknown_keys():
    table = {"a": "x.a", "b": "x.b"}
    assert sorting.parse("a,b", "asc,desc", table, "a") == [
        {"key": "a", "dir": "asc"},
        {"key": "b", "dir": "desc"},
    ]
    assert sorting.parse("a,b", "asc", table, "a") == [
        {"key": "a", "dir": "asc"},
        {"key": "b", "dir": "asc"},
    ]
    assert sorting.parse("nope,b", "desc", table, "a") == [{"key": "b", "dir": "desc"}]
    assert sorting.parse("nope", "asc", table, "a") == [{"key": "a", "dir": "asc"}]
    assert sorting.clause(sorting.parse("a,b", "asc,desc", table, "a"), table) == (
        "x.a ASC NULLS LAST, x.b DESC NULLS LAST"
    )


def test_board_sorts_by_company_then_date_posted(client, user_headers):
    uid = _uid(user_headers)
    d = datetime.date
    rows = [
        ("https://x.test/m1", "Beta", d(2026, 9, 1)),
        ("https://x.test/m2", "Alpha", d(2026, 9, 3)),
        ("https://x.test/m3", "Alpha", d(2026, 9, 5)),
        ("https://x.test/m4", "Alpha", None),
    ]
    for url, company, posted in rows:
        _insert_job("src-m", url, company=company, date_posted=posted)
        _pass_closed(url)
    _subscribe(uid, "src-m")

    body = client.get(
        "/v1/user/jobs",
        params={"sort": "company,date_posted", "dir": "asc,desc", "source": "src-m"},
        headers=user_headers,
    ).json()
    assert [r["url"] for r in body["rows"]] == [
        "https://x.test/m3",
        "https://x.test/m2",
        "https://x.test/m4",
        "https://x.test/m1",
    ]
    assert body["sorts"] == [
        {"key": "company", "dir": "asc"},
        {"key": "date_posted", "dir": "desc"},
    ]
    assert "comp" in body["sortable"]

    single = client.get(
        "/v1/user/jobs", params={"sort": "company", "source": "src-m"}, headers=user_headers
    ).json()
    assert single["sorts"] == [{"key": "company", "dir": "desc"}]
    assert single["rows"][0]["url"] == "https://x.test/m1"


def test_admin_jobs_accepts_the_same_shape(client, admin_headers):
    body = client.get(
        "/v1/admin/jobs",
        params={"sort": "company,checks", "dir": "asc,desc"},
        headers=admin_headers,
    ).json()
    assert body["sorts"] == [{"key": "company", "dir": "asc"}, {"key": "checks", "dir": "desc"}]
    assert (body["sort"], body["dir"]) == ("company", "asc")
