"""Why a filter rejects what it rejects, checked at the route.

filter_insights.py carried 600 lines and no test: nothing imported it and
nothing called its paths. The module's value is entirely in how it counts, so
these assert the counting rules its docstrings claim, through the same seam a
caller crosses rather than by reaching into the private helpers.
"""

from __future__ import annotations

import pytest

from core.reason_taxonomy import GROUPS

# Reasons chosen against the real taxonomy, not invented:
#   two groups at once, and evidence-missing
BOTH = "compensation undisclosed; role is senior staff level"
#   one group, not evidence-missing
SENIORITY = "requires 10 years of experience"
#   no group at all
NEITHER = "the moon is made of cheese"


_urls = iter(f"https://jobs.test/fi-{n}" for n in range(1, 10_000))


def _reject(f, prompt_hash: str, reason: str) -> str:
    """A rejected custom verdict on a job of its own. Returns the url so a
    caller can assert on distinct-job counts."""
    url = next(_urls)
    f.make_job(url=url)
    f.make_verdict(url, "custom", "rejected", prompt_hash=prompt_hash, reason=reason)
    return url


def _version(body: dict, prompt_hash: str) -> dict:
    (row,) = [v for v in body["prompt_versions"] if v["prompt_hash"] == prompt_hash]
    return row


def test_groups_names_every_key_in_the_taxonomy(client, admin_headers):
    """A group added to GROUPS without a GROUP_LABELS entry raises KeyError
    when a rejection lands in it, which is a long way from where it was added."""
    body = client.get("/v1/admin/filter-insights/groups", headers=admin_headers).json()

    assert {g["key"] for g in body["groups"]} == {g.key for g in GROUPS}
    assert all(g["label"] for g in body["groups"])
    assert body["evidence_missing_criterion"]


@pytest.mark.parametrize(
    "path,params",
    [
        ("/v1/admin/filter-insights/groups", {}),
        ("/v1/admin/filter-insights/rejection-reasons", {}),
        ("/v1/admin/filter-insights/phrasings", {"prompt_hash": "abc"}),
    ],
)
def test_the_admin_views_refuse_a_regular_user(client, user_headers, path, params):
    assert client.get(path, params=params, headers=user_headers).status_code == 403


def test_a_reason_in_two_groups_is_still_one_decision(client, admin_headers, f):
    """The documented invariant: a reason can land in several groups, so the
    evidence-missing total is carried per hash rather than summed from the
    groups. Summing would count this rejection twice."""
    filt = f.make_filter(f.make_user())
    _reject(f, filt["prompt_hash"], BOTH)

    body = client.get(
        "/v1/admin/filter-insights/rejection-reasons",
        params={"min_decisions": 1},
        headers=admin_headers,
    ).json()
    row = _version(body, filt["prompt_hash"])

    # One rejection, landing in two groups.
    assert row["totals"]["rejected"] == 1
    assert {g["key"] for g in row["groups"]} == {"pay_undisclosed", "seniority"}
    assert [g["decisions"] for g in row["groups"]] == [1, 1]

    # But counted once, which is the whole point.
    assert row["totals"]["evidence_missing_decisions"] == 1
    assert row["totals"]["evidence_missing_distinct_jobs"] == 1
    assert body["overlapping_groups"] is True


def test_a_reason_matching_no_group_lands_in_ungrouped(client, admin_headers, f):
    filt = f.make_filter(f.make_user())
    _reject(f, filt["prompt_hash"], NEITHER)

    row = _version(
        client.get(
            "/v1/admin/filter-insights/rejection-reasons",
            params={"min_decisions": 1},
            headers=admin_headers,
        ).json(),
        filt["prompt_hash"],
    )

    assert row["groups"] == []
    assert row["ungrouped"]["decisions"] == 1
    assert row["ungrouped"]["examples"] == [NEITHER]
    # Not evidence-missing, so it must not inflate that total.
    assert row["totals"]["evidence_missing_decisions"] == 0


def test_sufficient_tracks_min_decisions_and_the_threshold_ships_with_it(client, admin_headers, f):
    """A share is only worth rendering when one decision cannot swing it much.
    The caller gets the threshold back so the UI draws the same line."""
    filt = f.make_filter(f.make_user())
    for _ in range(3):
        _reject(f, filt["prompt_hash"], SENIORITY)

    def ask(min_decisions: int) -> dict:
        return _version(
            client.get(
                "/v1/admin/filter-insights/rejection-reasons",
                params={"min_decisions": min_decisions},
                headers=admin_headers,
            ).json(),
            filt["prompt_hash"],
        )

    assert ask(3)["sufficient"] is True
    assert ask(4)["sufficient"] is False
    # The row is still returned when insufficient; it is labelled, not hidden.
    assert ask(4)["totals"]["rejected"] == 3

    default = client.get(
        "/v1/admin/filter-insights/rejection-reasons", headers=admin_headers
    ).json()
    assert default["min_decisions"] == 50


def test_an_unowned_prompt_hash_says_unknown_rather_than_guessing(client, admin_headers, f):
    """Editing a prompt orphans every verdict the old one produced. The payload
    is required to say so rather than attribute them to whoever holds the
    current text."""
    _reject(f, "0" * 64, SENIORITY)

    row = _version(
        client.get(
            "/v1/admin/filter-insights/rejection-reasons",
            params={"min_decisions": 1},
            headers=admin_headers,
        ).json(),
        "0" * 64,
    )

    assert row["owner"]["state"] == "unknown"
    assert row["owner"]["user_count"] == 0
    # None, not False: no current filter carries this prompt, so "can it still
    # fire" is unanswerable rather than answered no.
    assert row["owner"]["enabled"] is None


def test_phrasings_drills_into_a_group_and_into_ungrouped(client, admin_headers, f):
    filt = f.make_filter(f.make_user())
    _reject(f, filt["prompt_hash"], SENIORITY)
    _reject(f, filt["prompt_hash"], NEITHER)

    def ask(group: str) -> dict:
        return client.get(
            "/v1/admin/filter-insights/phrasings",
            params={"prompt_hash": filt["prompt_hash"], "group": group},
            headers=admin_headers,
        ).json()

    seniority = ask("seniority")
    assert [p["phrasing"] for p in seniority["phrasings"]] == [SENIORITY]
    # ungrouped is drilled into by the same key it is reported under.
    assert [p["phrasing"] for p in ask("ungrouped")["phrasings"]] == [NEITHER]

    # A page states its own extent rather than implying it is the whole set.
    assert seniority["total_phrasings"] == 1
    assert seniority["returned"] == 1
    assert seniority["has_more"] is False
