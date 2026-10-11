"""Admin filter insight endpoint behavior."""

from api import db
from core.reason_taxonomy import GROUP_KEYS
from tests.filter_insights_support import _get, _phrasings, _reject


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
    for user_id, name, phash, enabled in (
        (one, "solo", "hash_solo", True),
        (one, "shared", "hash_shared", False),
        (two, "shared_too", "hash_shared", True),
    ):
        db.execute(
            "INSERT INTO user_filters (user_id, name, prompt, prompt_hash, enabled) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, name, "p", phash, enabled),
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


def test_phrasings_lists_every_distinct_wording_with_its_count(client, admin_headers):
    """The counts are the answer to "how is this derived": three sentences
    repeated fifty times and 159 near-unique ones mean different things about
    whether the grouping is doing real work."""
    for i in range(3):
        _reject(f"https://p.test/a{i}", "No pay disclosed.", "hash_aaaa", "f")
    _reject("https://p.test/b", "Salary not listed anywhere.", "hash_aaaa", "f")

    body = _phrasings(client, admin_headers, prompt_hash="hash_aaaa", group="pay_undisclosed")
    assert body["total_phrasings"] == 2
    assert body["total_decisions"] == 4
    assert body["phrasings"][0] == {
        "phrasing": "No pay disclosed.",
        "decisions": 3,
        "distinct_jobs": 3,
    }
    assert body["has_more"] is False


def test_phrasings_is_scoped_to_one_prompt_version(client, admin_headers):
    _reject("https://p.test/1", "No pay disclosed.", "hash_aaaa", "f")
    _reject("https://p.test/2", "No pay disclosed.", "hash_bbbb", "f")
    body = _phrasings(client, admin_headers, prompt_hash="hash_aaaa", group="pay_undisclosed")
    assert body["total_decisions"] == 1, "must not span prompt versions"


def test_phrasings_states_what_it_truncated(client, admin_headers):
    """Silently returning a page as though it were the whole set is the
    failure this endpoint exists to avoid."""
    for i in range(5):
        _reject(f"https://p.test/{i}", f"No pay disclosed, case {i}.", "hash_aaaa", "f")
    body = _phrasings(
        client, admin_headers, prompt_hash="hash_aaaa", group="pay_undisclosed", limit=2
    )
    assert body["total_phrasings"] == 5
    assert body["returned"] == 2
    assert body["has_more"] is True

    rest = _phrasings(
        client,
        admin_headers,
        prompt_hash="hash_aaaa",
        group="pay_undisclosed",
        limit=2,
        offset=4,
    )
    assert rest["returned"] == 1 and rest["has_more"] is False


def test_phrasings_can_list_the_ungrouped_residual(client, admin_headers):
    """The residual is a real bucket on the insight page, so it has to be
    drillable the same way a named group is."""
    _reject("https://p.test/1", "Zzzz qqqq wwww.", "hash_aaaa", "f")
    _reject("https://p.test/2", "No pay disclosed.", "hash_aaaa", "f")
    body = _phrasings(client, admin_headers, prompt_hash="hash_aaaa", group="ungrouped")
    assert body["total_phrasings"] == 1
    assert body["phrasings"][0]["phrasing"] == "Zzzz qqqq wwww."


def test_phrasings_without_a_group_covers_the_whole_version(client, admin_headers):
    _reject("https://p.test/1", "Zzzz qqqq wwww.", "hash_aaaa", "f")
    _reject("https://p.test/2", "No pay disclosed.", "hash_aaaa", "f")
    body = _phrasings(client, admin_headers, prompt_hash="hash_aaaa")
    assert body["total_phrasings"] == 2


def test_examples_are_a_deterministic_sample_not_a_frequency_ranking(client, admin_headers):
    """Nearly every phrasing occurs once, so ordering by count alone leaves the
    choice to row arrival order. Ties break on the text so the same three come
    back every time."""
    for i in range(4):
        _reject(f"https://p.test/{i}", f"No pay disclosed, variant {i}.", "hash_aaaa", "f")
    first = _get(client, admin_headers)["prompt_versions"][0]["groups"][0]["examples"]
    second = _get(client, admin_headers)["prompt_versions"][0]["groups"][0]["examples"]
    assert first == second
    assert first == sorted(first)


def test_examples_selection_is_published_so_it_cannot_be_narrated_wrong(client, admin_headers):
    """The field name was always right; the sentence wrapped around it on the
    page was not. Shipping the derivation means a caller renders it instead of
    recalling it."""
    _reject("https://p.test/1", "No pay disclosed.", "hash_aaaa", "f")
    sel = _get(client, admin_headers)["examples_selection"]
    assert sel["method"] == "sample"
    assert "not a frequency ranking" in sel["description"]
