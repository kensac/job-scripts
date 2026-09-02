"""Drill-through from a rejection-reason count to the rows behind it.

The aggregate classifies reasons in Python; this route filters in SQL. If the
two ever disagree, a count links to a different set of rows than it counted -
which is worse than not linking at all, because it looks like it worked.
"""

from __future__ import annotations

import pytest

from core import reason_taxonomy


@pytest.fixture
def reason_rows():
    from core.store import add_ai_result

    add_ai_result(
        "https://r.test/1",
        "rejected",
        reason="Salary not disclosed; cannot confirm the tier.",
        check_type="custom",
        model="gpt-5-nano",
        prompt_hash="h1",
    )
    add_ai_result(
        "https://r.test/2",
        "rejected",
        reason="Pay is $90,000, below the stated bar.",
        check_type="custom",
        model="gpt-5-nano",
        prompt_hash="h1",
    )
    add_ai_result(
        "https://r.test/3",
        "passed",
        reason="Backend role, compensation above the bar.",
        check_type="custom",
        model="gpt-5-nano",
        prompt_hash="h1",
    )


def test_unknown_group_is_400_not_500(client, admin_headers):
    """The keys are a closed server-side vocabulary, so a stale link is a bad
    request. sql_pattern raises KeyError, which would otherwise be a 500."""
    resp = client.get("/v1/admin/queries?reason_group=not_a_real_group", headers=admin_headers)
    assert resp.status_code == 400, resp.text
    body = resp.json()["detail"]
    assert body["code"] == "UNKNOWN_REASON_GROUP"
    assert "pay_undisclosed" in body["valid"]


def test_every_taxonomy_key_is_accepted(client, admin_headers):
    """Every key the aggregate can emit must be drillable. A group the UI
    renders but the router rejects is a dead link that looks live."""
    for group in reason_taxonomy.GROUPS:
        resp = client.get(f"/v1/admin/queries?reason_group={group.key}", headers=admin_headers)
        assert resp.status_code == 200, f"{group.key}: {resp.text}"


def test_sql_selection_matches_python_classification(client, admin_headers, reason_rows):
    """The equivalence that makes the link honest, asserted end to end rather
    than only on the pattern strings: Postgres spells the word boundary \\y
    where Python spells \\b, so the two regexes are not byte-identical and
    could drift apart without this."""
    for group in reason_taxonomy.GROUPS:
        resp = client.get(
            f"/v1/admin/queries?reason_group={group.key}&check_type=custom",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        sql_urls = {r["url"] for r in resp.json()["rows"]}

        expected = set()
        for url, reason in (
            ("https://r.test/1", "Salary not disclosed; cannot confirm the tier."),
            ("https://r.test/2", "Pay is $90,000, below the stated bar."),
            ("https://r.test/3", "Backend role, compensation above the bar."),
        ):
            if group.pattern.search(reason):
                expected.add(url)
        assert sql_urls == expected, f"{group.key} disagrees between SQL and Python"


def test_evidence_missing_composes_with_other_filters(client, admin_headers, reason_rows):
    resp = client.get(
        "/v1/admin/queries?evidence_missing=true&status=rejected&check_type=custom",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    urls = {r["url"] for r in resp.json()["rows"]}
    # "cannot confirm" is evidence-missing language; a stated-and-below-bar
    # rejection is the filter working and must not be swept in with it.
    assert "https://r.test/1" in urls
    assert "https://r.test/2" not in urls
