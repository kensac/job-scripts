from __future__ import annotations

USER_INSIGHT = "/v1/user/filter-insights/rejection-reasons"


def _reject(f, url: str, phash: str, reason: str) -> None:
    f.make_verdict(url, "custom", "rejected", prompt_hash=phash, reason=reason)


def test_a_user_sees_the_rejections_of_their_own_filter(client, user_headers, f):
    """The person who can fix a misfiring filter is its owner, and the only
    view of what it rejected was admin-only."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, name="mine", prompt="pay over 200k")
    for _ in range(3):
        _, url = f.make_ready_job(source="s")
        _reject(f, url, flt["prompt_hash"], "Salary not disclosed; cannot confirm the bar.")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    row = next(r for r in body["prompt_versions"] if r["prompt_hash"] == flt["prompt_hash"])
    assert [x["name"] for x in row["filters"]] == ["mine"]
    assert row["totals"]["rejected"] == 3
    assert row["totals"]["rejected_with_reason"] == 3
    assert sum(g["decisions"] for g in row["groups"]) >= 3


def test_a_user_never_sees_another_users_filter(client, user_headers, f):
    """Verdicts are shared by prompt_hash, so scoping has to come from the
    caller's own user_filters rows."""
    stranger = f.make_user()
    theirs = f.make_filter(stranger, name="not-yours", prompt="aerospace only")
    _, url = f.make_ready_job(source="s")
    _reject(f, url, theirs["prompt_hash"], "Not aerospace.")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    hashes = {r["prompt_hash"] for r in body["prompt_versions"]}
    assert theirs["prompt_hash"] not in hashes


def test_the_response_never_leaks_the_owner_embedded_in_filter_name(client, user_headers, f):
    """ai_queries.filter_name is `user1:pay_tier_200` - it embeds the owner.
    Two people adopting the same preset share verdicts, so echoing it would
    hand one of them the other's id and filter names."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, name="shared-preset", prompt="backend only")
    _, url = f.make_ready_job(source="s")
    from core.store import add_ai_result

    add_ai_result(
        url,
        "rejected",
        "Not backend.",
        "custom",
        prompt_hash=flt["prompt_hash"],
        filter_name="user999:someone-elses-name",
        model="gpt-5-nano",
    )

    raw = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).text
    assert "user999" not in raw
    assert "someone-elses-name" not in raw


def test_groups_report_the_denominator_they_are_actually_of(client, user_headers, f):
    """Buckets only cover rejections that carry a reason. The batched paths
    recorded none for a period, so dividing a group by `rejected` understates
    it - `rejected_with_reason` is the honest denominator."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, name="partly-reasoned", prompt="pay over 200k")
    _, with_reason = f.make_ready_job(source="s")
    _reject(f, with_reason, flt["prompt_hash"], "Salary not disclosed.")
    _, blank = f.make_ready_job(source="s")
    f.make_verdict(blank, "custom", "rejected", prompt_hash=flt["prompt_hash"], reason="")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    row = next(r for r in body["prompt_versions"] if r["prompt_hash"] == flt["prompt_hash"])
    assert row["totals"]["rejected"] == 2
    assert row["totals"]["rejected_with_reason"] == 1


def test_a_filter_that_decided_nothing_says_so_rather_than_vanishing(client, user_headers, f):
    """Absent reads as "no problems"; a zero row reads as "nothing ran"."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, name="never-ran", prompt="quantum only")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    row = next(r for r in body["prompt_versions"] if r["prompt_hash"] == flt["prompt_hash"])
    assert row["totals"]["evaluated"] == 0
    assert row["sufficient"] is False


def test_two_filter_names_sharing_a_prompt_are_one_row(client, user_headers, f):
    """One prompt here is both "default" and "general". A row per filter name
    would double-count the same decisions."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    a = f.make_filter(uid, name="default", prompt="same text")
    b = f.make_filter(uid, name="general", prompt="same text", enabled=False)
    assert a["prompt_hash"] == b["prompt_hash"]
    _, url = f.make_ready_job(source="s")
    _reject(f, url, a["prompt_hash"], "Nope.")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    rows = [r for r in body["prompt_versions"] if r["prompt_hash"] == a["prompt_hash"]]
    assert len(rows) == 1
    assert sorted(x["name"] for x in rows[0]["filters"]) == ["default", "general"]
    assert rows[0]["totals"]["rejected"] == 1
