"""Three lenses over the companies list, each a predicate over the same
aggregate the page computes, applied to the page and the total alike."""

from __future__ import annotations

import datetime

from api import db, signals
from tests.test_api_jobs import _insert_job


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


def _names(body: dict) -> set[str]:
    return {i["company_key"] if "company_key" in i else i["name"].lower() for i in body["items"]}


def test_applied_has_comp_and_repost_cut_the_list_and_the_total(client, admin_headers):
    admin = _uid(admin_headers)
    paid = _insert_job("src-cc", "https://x.test/cc1", company="Payco")
    db.execute("UPDATE jobs SET comp_extracted = true, comp_min = 100000 WHERE id = %s", (paid,))
    _insert_job("src-cc", "https://x.test/cc2", company="Appco")
    db.execute(
        "INSERT INTO applications (user_id, company_name, source_provenance, applied_at) "
        "VALUES (%s, 'Appco', 'tracker', now())",
        (admin,),
    )
    day = datetime.date(2026, 1, 1)
    for i in range(signals.REPOST_MIN_URLS):
        _insert_job(
            "src-cc",
            f"https://x.test/rp{i}",
            company="Repco",
            title="Same Role",
            locations=["Austin, TX"],
            date_posted=day + datetime.timedelta(days=i * (signals.REPOST_MIN_SPAN_DAYS + 1)),
        )
    _insert_job("src-cc", "https://x.test/cc9", company="Plainco")

    everything = client.get(
        "/v1/admin/companies", params={"limit": 200}, headers=admin_headers
    ).json()
    assert everything["filters"] == {} and everything["filterable"] == [
        "q",
        "applied",
        "has_comp",
        "repost",
    ]
    assert {"payco", "appco", "repco", "plainco"} <= _names(everything)

    for flag, expect in (("applied", "appco"), ("has_comp", "payco"), ("repost", "repco")):
        body = client.get(
            "/v1/admin/companies", params={flag: "true"}, headers=admin_headers
        ).json()
        assert _names(body) == {expect}, flag
        assert body["total_names"] == 1 and body["filters"] == {flag: ["true"]}

    both = client.get(
        "/v1/admin/companies", params={"has_comp": "true", "q": "pay"}, headers=admin_headers
    ).json()
    assert _names(both) == {"payco"} and both["filters"] == {"q": ["pay"], "has_comp": ["true"]}
