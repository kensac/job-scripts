from __future__ import annotations

from api import db
from core.reason_taxonomy import (
    EVIDENCE_MISSING_SQL,
    GROUP_KEYS,
    classify,
    is_evidence_missing,
    sql_pattern,
)
from core.store import add_ai_result

# ---------------------------------------------------------------------------
# taxonomy
# ---------------------------------------------------------------------------


def test_near_duplicate_phrasings_land_in_one_group():
    """The whole point of grouping: these are the same complaint written three
    ways, and an aggregate that keeps them apart says nothing."""
    for text in (
        "Salary not disclosed; cannot confirm $150k+ tier for new grads.",
        "Pay not listed; cannot confirm $150k+ total compensation for new grads.",
        "Compensation not stated; unable to confirm the pay tier.",
    ):
        assert "pay_undisclosed" in classify(text), text


def test_classification_is_multi_label():
    """A reason usually cites more than one thing. Forcing a single label would
    silently drop the other half of why a job was rejected."""
    keys = classify(
        "Senior Data Architect requiring 16 years experience; not appropriate "
        "for first-year new grad, plus no pay information."
    )
    assert "seniority" in keys
    assert "pay_undisclosed" in keys


def test_absent_pay_is_distinguished_from_stated_low_pay():
    """The distinction the whole feature exists to surface: a filter rejecting
    because the posting withheld a number is misfiring; one rejecting because
    the number was stated and too low is working."""
    absent = "No pay disclosed; unlikely to reach $200k first-year TC."
    stated = "Base salary range $66k-$82k; well below the $150k top-tier target."

    assert "pay_undisclosed" in classify(absent)
    assert is_evidence_missing(absent)

    assert "pay_below" in classify(stated)
    assert not is_evidence_missing(stated), "a disclosed number is not missing evidence"


def test_experience_ranges_match_with_either_dash():
    # The model writes both; an en dash slipping through unmatched would have
    # quietly undercounted the largest group in the corpus.
    assert "seniority" in classify("requires 2-5 years experience")
    assert "seniority" in classify("requires 2\u20135 years experience")


def test_unmatched_reason_classifies_as_nothing_rather_than_guessing():
    # Assigning a real reason to no group is the honest outcome; the residual
    # is reported rather than forced into the nearest-looking bucket.
    assert classify("Role is regulatory/compliance work.") == ()
    assert classify("") == ()
    assert classify(None) == ()


# A sample drawn from the production corpus, kept deliberately varied in
# phrasing rather than picked to pass. Coverage over the full 14,084 rejections
# measured 95.4%; this floor is what stops an edit to the patterns regressing
# that without anyone noticing.
_CORPUS_SAMPLE = (
    "Non-engineering trade role (plumbing technician); does not match software/backend criteria.",
    "Senior Data Architect requiring 16 years experience; not appropriate for new grad.",
    "Base salary tops at $65k; no equity/bonus; does not meet $200k first-year TC bar.",
    "No pay disclosed; HR services firm typically pays well below $200k first-year TC.",
    "Low overlap; role focuses on occupational safety data analysis.",
    "Role is IT/BA-focused with end-user support; lacks core backend or cloud infra overlap.",
    "Job requires PhD; candidate's education shows BS, no PhD.",
    "Role is data conversion/analyst, not engineering (backend/infrastructure).",
    "Hourly pay $18 translates to ~$37k/year; not plausibly $200k+ TC for a new grad.",
    "Bar A fail: starting salary $108k; unclear total comp to reach $200k.",
    "2+ years experience requirement for Product Owner; compensation under $150k.",
    "BAR B fails: role is hardware silicon (ASIC/RTL/FPGA); candidate is software-focused.",
    "No pay stated and consumer networking vendor unlikely to pay $200k+ for entry hardware design.",
    "Postdoc role requires PhD; pay range $72,879-$121,465 is below $150k top tier.",
    "Company not in elite tier; data annotation role is not a top-tier engineering position.",
    "Salary range $66k-$82k; well below the $150k top-tier target.",
    "Top base $85k (<$150k); BAR A fails.",
    "BAR A fails: top base $121,600 < $150k threshold for clearing pay.",
    "Non-tech civil construction role; not US-based or remote, outside user criteria.",
    "Employer is a staffing/recruiting agency, not a direct tech company.",
    "Base pay is $21-$23/hr, well below $150k/year top tier for new grads.",
    "Pay info missing; cannot confirm BAR A threshold of $200k.",
    "Role centers on medical device QA/hardware testing; lacks software overlap.",
    "Non-software/engineering role (assembly/test technician); not aligned with candidate profile.",
    "Outside US, onsite role; non-software position.",
)


def test_reason_taxonomy_coverage_on_representative_sample():
    placed = [r for r in _CORPUS_SAMPLE if classify(r)]
    coverage = len(placed) / len(_CORPUS_SAMPLE)
    assert coverage >= 0.90, (
        f"taxonomy placed only {coverage:.0%} of the sample; "
        f"unplaced: {[r for r in _CORPUS_SAMPLE if not classify(r)]}"
    )


def test_every_group_key_is_reachable_from_the_sample():
    """A pattern nothing can match is dead weight that still reads as a real
    category to whoever renders it."""
    seen = {k for r in _CORPUS_SAMPLE for k in classify(r)}
    assert set(GROUP_KEYS) - seen == set()


def test_sql_patterns_select_exactly_what_classify_labels(client, admin_headers):
    """The drill-through filters in SQL while the aggregate classifies in
    Python. If those two ever disagree, a count links to a different set of
    rows than it counted, which is worse than not linking at all."""
    for i, reason in enumerate(_CORPUS_SAMPLE):
        _reject(f"https://j.test/{i}", reason, "hash_aaaa", "f")

    for key in GROUP_KEYS:
        in_python = {
            f"https://j.test/{i}" for i, r in enumerate(_CORPUS_SAMPLE) if key in classify(r)
        }
        in_sql = {
            row["url"]
            for row in db.query(
                "SELECT url FROM ai_queries WHERE reason ~* %s", (sql_pattern(key),)
            )
        }
        assert in_sql == in_python, key

    missing_python = {
        f"https://j.test/{i}" for i, r in enumerate(_CORPUS_SAMPLE) if is_evidence_missing(r)
    }
    missing_sql = {
        row["url"]
        for row in db.query(
            "SELECT url FROM ai_queries WHERE reason ~* %s", (EVIDENCE_MISSING_SQL,)
        )
    }
    assert missing_sql == missing_python


def test_sql_pattern_rejects_an_unknown_key():
    import pytest

    with pytest.raises(KeyError):
        sql_pattern("no_such_group")


# ---------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------


def _reject(url: str, reason: str, prompt_hash: str, filter_name: str) -> None:
    add_ai_result(
        url,
        "rejected",
        reason,
        "custom",
        prompt_hash=prompt_hash,
        filter_name=filter_name,
    )


def _get(client, admin_headers, **params):
    resp = client.get(
        "/v1/admin/filter-insights/rejection-reasons",
        params={"min_decisions": 1, **params},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_row_is_a_prompt_version_not_a_filter(client, admin_headers):
    """Same name, two prompts. One row per name would average two different
    filters together and look entirely plausible doing it."""
    _reject("https://j.test/1", "No pay disclosed.", "hash_aaaa", "pay_tier_200")
    _reject("https://j.test/2", "Salary not listed.", "hash_bbbb", "pay_tier_200")

    body = _get(client, admin_headers)
    rows = {r["prompt_hash"]: r for r in body["prompt_versions"]}
    assert set(rows) == {"hash_aaaa", "hash_bbbb"}
    # Each row must say, per name, that the name spans another version.
    assert rows["hash_aaaa"]["sibling_hashes_by_name"] == {"pay_tier_200": 1}
    assert rows["hash_bbbb"]["sibling_hashes_by_name"] == {"pay_tier_200": 1}


def test_sibling_count_is_per_name_when_a_prompt_has_several(client, admin_headers):
    """One prompt has been called "default", "general" and "user1:default";
    a single number could not say which name it described."""
    _reject("https://j.test/1", "No pay disclosed.", "hash_aaaa", "default")
    _reject("https://j.test/2", "No pay disclosed.", "hash_aaaa", "general")
    _reject("https://j.test/3", "Salary not listed.", "hash_bbbb", "default")

    body = _get(client, admin_headers)
    rows = {r["prompt_hash"]: r for r in body["prompt_versions"]}
    assert rows["hash_aaaa"]["sibling_hashes_by_name"] == {"default": 1, "general": 0}


def test_group_counts_exceed_rejections_because_groups_overlap(client, admin_headers):
    _reject(
        "https://j.test/1",
        "Requires 5 years experience and no pay disclosed; not an engineering role.",
        "hash_aaaa",
        "f",
    )
    body = _get(client, admin_headers)
    row = body["prompt_versions"][0]
    assert row["totals"]["rejected"] == 1
    assert sum(g["decisions"] for g in row["groups"]) > row["totals"]["rejected"]
    assert body["overlapping_groups"] is True


def test_evidence_missing_is_not_the_sum_of_its_groups(client, admin_headers):
    """One decision in three groups is still one decision; summing the groups'
    evidence-missing counts would triple it."""
    _reject(
        "https://j.test/1",
        "Requires 5 years experience and no pay disclosed; not an engineering role.",
        "hash_aaaa",
        "f",
    )
    row = _get(client, admin_headers)["prompt_versions"][0]
    assert row["totals"]["evidence_missing_decisions"] == 1
    assert sum(g["evidence_missing_decisions"] for g in row["groups"]) > 1


def test_counts_are_decisions_and_distinct_jobs_differ(client, admin_headers):
    """Re-evaluation appends, so the same url is decided more than once; the
    payload has to let a caller label which it is rendering."""
    _reject("https://j.test/1", "No pay disclosed.", "hash_aaaa", "f")
    _reject("https://j.test/1", "Salary not listed.", "hash_aaaa", "f")
    row = _get(client, admin_headers)["prompt_versions"][0]
    assert row["totals"]["rejected"] == 2
    assert row["totals"]["distinct_jobs_rejected"] == 1


def test_owner_states_resolved_shared_and_unknown(client, admin_headers, f):
    one = f.make_user(email="one@example.test")
    two = f.make_user(email="two@example.test")
    for user_id, name, phash in (
        (one, "solo", "hash_solo"),
        (one, "shared", "hash_shared"),
        (two, "shared_too", "hash_shared"),
    ):
        db.execute(
            "INSERT INTO user_filters (user_id, name, prompt, prompt_hash) VALUES (%s, %s, %s, %s)",
            (user_id, name, "p", phash),
        )
    _reject("https://j.test/1", "No pay disclosed.", "hash_solo", "solo")
    _reject("https://j.test/2", "No pay disclosed.", "hash_shared", "shared")
    _reject("https://j.test/3", "No pay disclosed.", "hash_gone", "edited_away")

    rows = {r["prompt_hash"]: r for r in _get(client, admin_headers)["prompt_versions"]}
    assert rows["hash_solo"]["owner"]["state"] == "resolved"
    assert rows["hash_shared"]["owner"]["state"] == "shared"
    assert rows["hash_shared"]["owner"]["user_count"] == 2
    # An edited or deleted filter is unattributable, and the payload says so
    # rather than offering a nullable email that reads as a fact.
    assert rows["hash_gone"]["owner"]["state"] == "unknown"
    assert rows["hash_gone"]["owner"]["users"] == []


def test_ungrouped_residual_is_surfaced_not_dropped(client, admin_headers):
    _reject("https://j.test/1", "Zzzz qqqq wwww.", "hash_aaaa", "f")
    row = _get(client, admin_headers)["prompt_versions"][0]
    assert row["groups"] == []
    assert row["ungrouped"]["decisions"] == 1


def test_sufficient_flag_uses_the_returned_threshold(client, admin_headers):
    _reject("https://j.test/1", "No pay disclosed.", "hash_aaaa", "f")
    body = _get(client, admin_headers, min_decisions=5)
    assert body["min_decisions"] == 5
    assert body["prompt_versions"][0]["sufficient"] is False


def test_criterion_is_published_so_the_number_can_be_described(client, admin_headers):
    _reject("https://j.test/1", "No pay disclosed.", "hash_aaaa", "f")
    body = _get(client, admin_headers)
    criterion = body["evidence_missing_criterion"]
    assert criterion["method"] == "phrase_match"
    assert "not disclosed" in criterion["phrases"]


def test_requires_admin(client, user_headers):
    resp = client.get("/v1/admin/filter-insights/rejection-reasons", headers=user_headers)
    assert resp.status_code in (401, 403)


def test_a_drill_link_opens_exactly_the_rows_its_count_counted(client, admin_headers):
    """The property the whole drill-through exists to have. A count on the
    insight page is scoped to ONE prompt version; the link has to carry that
    scope or it opens the same reason group across every version, and the
    number the user clicked is not the number they land on.

    Without the prompt_hash filter this fails by exactly the rows belonging to
    the other version.
    """
    for i in range(3):
        _reject(f"https://drill.test/a{i}", "No pay disclosed.", "hash_aaaa", "pay_tier")
    for i in range(5):
        _reject(f"https://drill.test/b{i}", "Salary not listed.", "hash_bbbb", "pay_tier")

    insight = _get(client, admin_headers)
    version_a = next(r for r in insight["prompt_versions"] if r["prompt_hash"] == "hash_aaaa")
    group = next(g for g in version_a["groups"] if g["key"] == "pay_undisclosed")
    assert group["decisions"] == 3

    # Exactly the link the UI builds, including check_type so the
    # (check_type, prompt_hash) index serves the scope.
    drilled = client.get(
        "/v1/admin/queries",
        params={
            "check_type": "custom",
            "status": "rejected",
            "prompt_hash": "hash_aaaa",
            "reason_group": "pay_undisclosed",
            "page_size": 100,
        },
        headers=admin_headers,
    )
    assert drilled.status_code == 200, drilled.text
    body = drilled.json()
    assert body["total"] == group["decisions"], (
        "the drill link must open exactly the rows the count counted"
    )
    assert {r["url"] for r in body["rows"]} == {f"https://drill.test/a{i}" for i in range(3)}


def test_prompt_hash_filter_composes_with_the_reason_group(client, admin_headers):
    _reject("https://drill.test/1", "No pay disclosed.", "hash_aaaa", "f")
    _reject("https://drill.test/2", "Requires 5 years experience.", "hash_aaaa", "f")
    _reject("https://drill.test/3", "No pay disclosed.", "hash_bbbb", "f")

    resp = client.get(
        "/v1/admin/queries",
        params={"prompt_hash": "hash_aaaa", "reason_group": "pay_undisclosed"},
        headers=admin_headers,
    )
    assert resp.json()["total"] == 1, "both predicates must apply, not either"


def test_one_user_with_two_identical_filters_is_not_two_owners(client, admin_headers, f):
    """Counting user_filters rows instead of people reported a prompt as shared
    between two users when it belongs to one who wrote it twice. That is a
    claim about who to go and talk to, so it has to be right."""
    owner = f.make_user(email="solo@example.test")
    for name in ("default", "general"):
        db.execute(
            "INSERT INTO user_filters (user_id, name, prompt, prompt_hash, enabled) "
            "VALUES (%s, %s, %s, %s, %s)",
            (owner, name, "p", "hash_twice", False),
        )
    _reject("https://own.test/1", "No pay disclosed.", "hash_twice", "default")

    row = _get(client, admin_headers)["prompt_versions"][0]
    assert row["owner"]["state"] == "resolved"
    assert row["owner"]["user_count"] == 1
    assert [u["email"] for u in row["owner"]["users"]] == ["solo@example.test"]
    # The two filter rows are still visible, just not miscounted as people.
    assert [x["name"] for x in row["owner"]["filters"]] == ["default", "general"]


def test_enabled_says_whether_this_prompt_can_still_reject_anything(client, admin_headers, f):
    """`resolved` does not answer it: a disabled filter is still a current row,
    so a retired prompt resolves to its owner exactly like a live one. A page
    ranking by misfire rate would otherwise lead with a filter that cannot
    fire."""
    user = f.make_user()
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash, enabled) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user, "live", "p", "hash_live", True),
    )
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash, enabled) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user, "retired", "p2", "hash_retired", False),
    )
    _reject("https://own.test/1", "No pay disclosed.", "hash_live", "live")
    _reject("https://own.test/2", "No pay disclosed.", "hash_retired", "retired")
    _reject("https://own.test/3", "No pay disclosed.", "hash_gone", "vanished")

    rows = {r["prompt_hash"]: r for r in _get(client, admin_headers)["prompt_versions"]}
    assert rows["hash_live"]["owner"]["enabled"] is True
    assert rows["hash_retired"]["owner"]["enabled"] is False
    # Both resolve to an owner; only `enabled` separates them.
    assert rows["hash_live"]["owner"]["state"] == "resolved"
    assert rows["hash_retired"]["owner"]["state"] == "resolved"
    # No current filter carries this prompt, so there is nothing to ask.
    assert rows["hash_gone"]["owner"]["enabled"] is None


def test_groups_endpoint_resolves_a_drill_key_to_its_label(client, admin_headers):
    """A drill link carries only the key. Baking the label into the URL instead
    would go stale the moment the taxonomy is edited."""
    resp = client.get("/v1/admin/filter-insights/groups", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    labels = {g["key"]: g["label"] for g in body["groups"]}

    assert set(labels) == set(GROUP_KEYS)
    # The cases a humanised key would lose: these groups are broader than
    # their key reads.
    assert labels["seniority"] == "Seniority or experience mismatch"
    assert labels["location"] == "Location or work authorisation"
    assert labels["pay_below"] == "Pay below threshold"
    assert body["evidence_missing_criterion"]["method"] == "phrase_match"


def test_groups_endpoint_requires_admin(client, user_headers):
    resp = client.get("/v1/admin/filter-insights/groups", headers=user_headers)
    assert resp.status_code in (401, 403)
