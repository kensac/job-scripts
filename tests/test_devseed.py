"""The dev seed, asserted THROUGH THE API rather than against the tables.

That is the whole point. A fixture cannot falsify the assumption it was built
from, so these tests read the seed back over the wire: if a response shape
drifts from what the seed produces, the drift shows up here rather than in a
frontend built against a stale belief.

Each assertion names the production distribution it stands for, because a seed
of tidy rows would reproduce none of the bugs the mock layer let through.
"""

from __future__ import annotations

import pytest

from api import db
from core.devseed import DEV_SUB, seed


@pytest.fixture
def seeded(client):
    counts = seed()
    assert counts["jobs"] and counts["messages"] and counts["applications"]
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (DEV_SUB,))
    assert row is not None
    return row["id"]


@pytest.fixture
def dev_headers(client, seeded):
    import os

    return {
        "X-Service-Token": os.environ["JOBTRACKER_SERVICE_TOKEN"],
        "X-User-Sub": DEV_SUB,
        "X-User-Email": "dev@example.test",
        "X-User-Name": "Dev User",
        "X-User-Groups": "infra-admins,jobtracker-users-internal",
    }


def test_seeding_twice_does_not_duplicate(client):
    first = seed()
    before = db.query_one("SELECT count(*) AS n FROM users WHERE sub = %s", (DEV_SUB,))
    seed()
    after = db.query_one("SELECT count(*) AS n FROM users WHERE sub = %s", (DEV_SUB,))
    assert first["jobs"] and before == after


def test_it_refuses_to_seed_a_database_not_named_disposable(client, monkeypatch):
    """A dev API that can reach production is worse than no dev API, and the
    database NAME is the one thing a caller cannot get wrong by accident."""
    monkeypatch.setattr(db, "query_one", lambda *a, **k: {"name": "jobtracker"})
    with pytest.raises(RuntimeError, match="refusing to seed"):
        seed()


def test_a_job_can_be_inactive_while_the_closed_check_says_open(client, dev_headers):
    """114 real applications sit in exactly this state. Reading `active` as
    closure is what put a red badge on all of them."""
    rows = client.get("/v1/user/jobs?limit=100", headers=dev_headers).json()["rows"]
    contradictory = [r for r in rows if r["active"] is False and r["closed_verdict"] == "open"]
    assert contradictory, "the seed must carry the shape that caused the badge bug"


def test_never_checked_is_a_third_state_over_the_wire(client, dev_headers):
    rows = client.get("/v1/user/jobs?limit=100", headers=dev_headers).json()["rows"]
    assert any(r["closed_verdict"] is None for r in rows), "NULL must not render as closed"
    assert any(r["closed_verdict"] == "closed" for r in rows)


def test_an_amount_can_arrive_without_a_currency(client, dev_headers):
    """96% of real rows carrying an amount have no currency. A renderer that
    assumes USD turns a CAD range into a dollar figure."""
    rows = client.get("/v1/user/jobs?limit=100", headers=dev_headers).json()["rows"]
    priced = [r for r in rows if r["comp_min"] is not None]
    assert priced
    assert any(r["comp_currency"] is None for r in priced)
    assert any(r["comp_currency"] for r in priced), "and some do carry one"


def test_a_message_arrives_as_sanitised_markup_with_its_trackers_withheld(client, dev_headers):
    """72% of real messages carry at least one remote tracker."""
    ids = [
        r["id"]
        for r in db.query("SELECT id FROM email_messages WHERE body_html IS NOT NULL ORDER BY id")
    ]
    assert ids, "the seed must include a message with markup"
    body = client.get(f"/v1/user/messages/{ids[0]}", headers=dev_headers).json()
    assert body["blocked_remote_content"] >= 1
    assert "data-blocked-src" in body["body_html"]
    assert "<script" not in (body["body_html"] or "")


def test_confidence_is_a_string_and_not_every_event_is_high(client, dev_headers):
    """It is 'high' / 'medium' / 'low' in production - 81,788 / 138 / 22. A
    client typing it as a number breaks on the first medium."""
    rows = db.query("SELECT DISTINCT confidence FROM email_events")
    values = {r["confidence"] for r in rows}
    assert values >= {"high", "medium", "low"}
    assert all(isinstance(v, str) for v in values)


def test_role_title_is_a_key_that_exists_and_is_null(client, dev_headers):
    """Present as a key on 81,000 rows and NULL as a value on 73,892 of them.
    A client that checks `'role_title' in detail` reads it as present."""
    rows = db.query("SELECT detail FROM email_events WHERE detail ? 'role_title'")
    assert rows
    assert any(r["detail"]["role_title"] is None for r in rows)
    assert any(r["detail"]["role_title"] for r in rows)


def test_two_filters_share_one_prompt_hash(client, dev_headers):
    """ "default" and "general" are one prompt in production. A row per name
    double counts the same decisions."""
    body = client.get(
        "/v1/user/filter-insights/rejection-reasons?min_decisions=1", headers=dev_headers
    ).json()
    shared = [v for v in body["prompt_versions"] if len(v["filters"]) > 1]
    assert shared, "the seed must carry two names on one prompt"
    assert {f["name"] for f in shared[0]["filters"]} == {"default", "general"}


def test_a_rejection_can_carry_no_reason(client, dev_headers):
    """The batched paths recorded none for weeks, so the share denominator is
    rejected_with_reason and not rejected."""
    body = client.get(
        "/v1/user/filter-insights/rejection-reasons?min_decisions=1", headers=dev_headers
    ).json()
    totals = body["prompt_versions"][0]["totals"]
    assert totals["rejected"] > totals["rejected_with_reason"]


def test_a_configured_source_can_have_no_postings(client, dev_headers):
    rows = client.get("/v1/admin/sources", headers=dev_headers).json()["sources"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["silent_board"]["jobs"] == 0
    assert by_name["silent_board"]["last_new_posting_at"] is None


def test_no_open_action_item_has_a_future_deadline(client, dev_headers):
    """The real corpus is ~99% historical and has zero future deadlines. A
    notifier built against tidy future-dated fixtures would look useful and be
    wrong on every real row."""
    rows = db.query("SELECT due_at FROM action_items WHERE resolved_at IS NULL")
    assert rows
    assert all(
        r["due_at"] is None
        or r["due_at"] < __import__("datetime").datetime.now(__import__("datetime").UTC)
        for r in rows
    )
