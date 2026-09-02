from __future__ import annotations

from api import db, signals
from api.routers.companies import OPEN_MIN_CHECKED

ENDPOINT = "/v1/admin/companies"


def _items(client, headers, query: str = "") -> list[dict]:
    resp = client.get(f"{ENDPOINT}{query}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _item(client, headers, key: str, query: str = "") -> dict:
    row = next((i for i in _items(client, headers, query) if i["company_key"] == key), None)
    assert row is not None, f"{key} missing"
    return row


def _set_comp(job_id: int, *, extracted: bool, low=None, high=None, currency=None) -> None:
    db.execute(
        "UPDATE jobs SET comp_extracted = %s, comp_min = %s, comp_max = %s, comp_currency = %s "
        "WHERE id = %s",
        (extracted, low, high, currency, job_id),
    )


def test_requires_admin(client, user_headers):
    assert client.get(ENDPOINT, headers=user_headers).status_code == 403


def test_names_are_casefolded_into_one_row(client, admin_headers, f):
    """Identity is lower(btrim(company)) and nothing more, so spelling variants
    of the same string collapse - but 'Acme, Inc.' stays a separate company."""
    f.make_job(source="s", company="Acme", title="A")
    f.make_job(source="s", company="  ACME ", title="B")
    f.make_job(source="s", company="Acme, Inc.", title="C")

    assert _item(client, admin_headers, "acme")["total_postings_seen"] == 2
    assert _item(client, admin_headers, "acme, inc.")["total_postings_seen"] == 1


def test_display_name_is_the_most_common_spelling(client, admin_headers, f):
    for _ in range(3):
        f.make_job(source="s", company="Acme", title="x")
    f.make_job(source="s", company="ACME", title="y")

    assert _item(client, admin_headers, "acme")["company_name"] == "Acme"


def test_comp_separates_found_from_ran_and_found_nothing(client, admin_headers, f):
    """The distinction the whole comp block exists for: 'we looked and found
    no pay' is a different fact from 'we never looked', and neither is a claim
    about what the employer published."""
    found = f.make_job(source="s", company="Payco", title="a")
    ran_empty = f.make_job(source="s", company="Payco", title="b")
    f.make_job(source="s", company="Payco", title="c")
    _set_comp(found, extracted=True, low=100000, high=150000, currency="USD")
    _set_comp(ran_empty, extracted=True)

    row = _item(client, admin_headers, "payco")
    assert row["extraction"] == {"found_pay": 1, "ran_found_nothing": 1, "not_attempted": 1}
    assert (
        row["extraction"]["found_pay"]
        + row["extraction"]["ran_found_nothing"]
        + row["extraction"]["not_attempted"]
        == row["total_postings_seen"]
    )
    assert row["comp"]["n_extracted"] == 1
    assert row["comp"]["n_total"] == 3


def test_amounts_without_a_currency_are_not_folded_into_a_currency(client, admin_headers, f):
    """452 of 11,442 extracted rows carry a currency. Assuming the rest are
    USD would invent a figure; they stay in their own null bucket."""
    usd = f.make_job(source="s", company="Mixed", title="a")
    unknown = f.make_job(source="s", company="Mixed", title="b")
    _set_comp(usd, extracted=True, low=100000, high=120000, currency="USD")
    _set_comp(unknown, extracted=True, low=90000, high=95000)

    buckets = {
        c["currency"]: c for c in _item(client, admin_headers, "mixed")["comp"]["by_currency"]
    }
    assert buckets.keys() == {"USD", None}
    assert buckets[None]["n"] == 1
    assert buckets["USD"]["n"] == 1


def test_applications_are_absent_rather_than_zero(client, admin_headers, f):
    f.make_job(source="s", company="Untouched", title="a")

    assert "applications" not in _item(client, admin_headers, "untouched")


def test_applications_carry_their_status_breakdown(client, admin_headers, f):
    """The count comes from `applications`, which is the entity; the status
    breakdown still comes from the board, because a tracker status is a thing
    the user typed and only tracked postings have one."""
    user_id = f.make_user()
    job_id = f.make_job(source="s", company="Applied", title="a")
    f.make_board_row(user_id, job_id, status="Application Submitted")
    db.execute("UPDATE user_jobs SET date_applied = now() WHERE job_id = %s", (job_id,))
    db.execute(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "applied_at) VALUES (%s, %s, 'Applied', 'a', 'tracker', now())",
        (user_id, job_id),
    )

    apps = _item(client, admin_headers, "applied")["applications"]
    assert apps["n"] == 1
    assert apps["statuses"] == {"Application Submitted": 1}
    assert apps["last_applied_at"] is not None


def test_an_application_only_email_knows_about_still_counts(client, admin_headers, f):
    """The reason for repointing this at `applications`: a 2022 application has
    no posting in the catalog and no board row, and the old query could not see
    it. That reported 605 companies when 1,283 had evidence."""
    user_id = f.make_user()
    f.make_job(source="s", company="Ghosted", title="a")
    db.execute(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "applied_at) VALUES (%s, NULL, 'Ghosted', 'a', 'email', now())",
        (user_id,),
    )

    apps = _item(client, admin_headers, "ghosted")["applications"]
    assert apps["n"] == 1
    assert apps["statuses"] == {}, "no board row means no tracker status, not a missing count"


def test_a_dismissed_application_stops_counting(client, admin_headers, f):
    """A dismissal says the row should never have existed. Counting it would be
    counting a known mistake."""
    user_id = f.make_user()
    f.make_job(source="s", company="Coursework", title="a")
    db.execute(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "applied_at, dismissed_at) VALUES (%s, NULL, 'Coursework', 'a', 'email', now(), now())",
        (user_id,),
    )

    assert "applications" not in _item(client, admin_headers, "coursework")


def test_open_is_omitted_below_its_floor(client, admin_headers, f):
    for i in range(OPEN_MIN_CHECKED - 1):
        f.make_ready_job(source="s", company="Thin", title=f"t{i}", closed="passed")

    assert "open" not in _item(client, admin_headers, "thin")


def test_open_comes_from_the_closed_check_not_the_active_flag(client, admin_headers, f):
    """`active` reports whether a board still lists a posting. Deriving open
    from it would rank companies by which board scraped them."""
    for i in range(OPEN_MIN_CHECKED):
        job_id, _ = f.make_ready_job(
            source="s", company="Checked", title=f"t{i}", closed="rejected" if i < 2 else "passed"
        )
        db.execute("UPDATE jobs SET active = FALSE WHERE id = %s", (job_id,))

    row = _item(client, admin_headers, "checked")
    assert row["open"]["n_checked"] == OPEN_MIN_CHECKED
    assert row["open"]["n_open"] == OPEN_MIN_CHECKED - 2
    assert row["open"]["last_checked_at"] is not None


def test_open_counts_the_latest_verdict_only(client, admin_headers, f):
    for i in range(OPEN_MIN_CHECKED):
        _, url = f.make_ready_job(source="s", company="Revisited", title=f"t{i}", closed="passed")
        if i == 0:
            f.make_verdict(url, "closed", "rejected")

    row = _item(client, admin_headers, "revisited")
    assert row["open"]["n_checked"] == OPEN_MIN_CHECKED
    assert row["open"]["n_open"] == OPEN_MIN_CHECKED - 1


def test_repost_uses_the_same_definition_as_the_per_job_signal(client, admin_headers, f):
    """Both surfaces import the floors from api.signals, so a repost cannot
    mean one thing on the company page and another in the drawer."""
    first = f.make_job(source="board", company="Reposter", title="Engineer")
    second = f.make_job(source="board", company="Reposter", title="Engineer")
    db.execute(
        "UPDATE jobs SET date_posted = now() - make_interval(days => %s) WHERE id = %s",
        (signals.REPOST_MIN_SPAN_DAYS * 4, first),
    )
    db.execute("UPDATE jobs SET date_posted = now() WHERE id = %s", (second,))

    repost = _item(client, admin_headers, "reposter")["repost"]
    assert repost["url_count"] == 2
    assert repost["title"] == "Engineer"
    assert repost["span_days"] >= signals.REPOST_MIN_SPAN_DAYS


def test_repost_excludes_a_role_listed_across_many_locations(client, admin_headers, f):
    """The defect this fixed: keyed without location, one chain's role across
    120 stores grouped into a single 1,056-url "repost"."""
    first = f.make_job(source="chain", company="Grocer", title="Assistant")
    second = f.make_job(source="chain", company="Grocer", title="Assistant")
    db.execute("UPDATE jobs SET locations = %s WHERE id = %s", (["Leeds, UK"], first))
    db.execute("UPDATE jobs SET locations = %s WHERE id = %s", (["Bath, UK"], second))
    db.execute(
        "UPDATE jobs SET date_posted = now() - make_interval(days => %s) WHERE id = %s",
        (signals.REPOST_MIN_SPAN_DAYS * 4, first),
    )
    db.execute("UPDATE jobs SET date_posted = now() WHERE id = %s", (second,))

    assert "repost" not in _item(client, admin_headers, "grocer")


def test_repost_excludes_the_same_role_on_two_boards(client, admin_headers, f):
    first = f.make_job(source="board-a", company="Syndicated", title="Engineer")
    second = f.make_job(source="board-b", company="Syndicated", title="Engineer")
    db.execute(
        "UPDATE jobs SET date_posted = now() - make_interval(days => %s) WHERE id = %s",
        (signals.REPOST_MIN_SPAN_DAYS * 4, first),
    )
    db.execute("UPDATE jobs SET date_posted = now() WHERE id = %s", (second,))

    assert "repost" not in _item(client, admin_headers, "syndicated")


def test_search_matches_on_the_casefolded_key(client, admin_headers, f):
    f.make_job(source="s", company="Northwind", title="a")
    f.make_job(source="s", company="Southwind", title="a")

    keys = {i["company_key"] for i in _items(client, admin_headers, "?q=NORTH")}
    assert keys == {"northwind"}


def test_sorting_is_applied_across_the_whole_set_not_the_page(client, admin_headers, f):
    """A page-local sort would reorder one slice and present it as a ranking."""
    for n, name in ((5, "Big"), (3, "Mid"), (1, "Small")):
        for i in range(n):
            f.make_job(source="s", company=name, title=f"t{i}")

    first_page = _items(client, admin_headers, "?sort=total_postings_seen&dir=desc&limit=1")
    assert [i["company_key"] for i in first_page] == ["big"]
    ascending = _items(client, admin_headers, "?sort=total_postings_seen&dir=asc&limit=1")
    assert [i["company_key"] for i in ascending] == ["small"]


def test_paging_walks_the_whole_set_without_repeating(client, admin_headers, f):
    for i in range(7):
        f.make_job(source="s", company=f"Co{i:02d}", title="a")

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        query = f"?limit=3&sort=company_name&dir=asc{f'&cursor={cursor}' if cursor else ''}"
        payload = client.get(f"{ENDPOINT}{query}", headers=admin_headers).json()
        seen.extend(i["company_key"] for i in payload["items"])
        cursor = payload["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 7


def test_an_unparsable_cursor_is_rejected(client, admin_headers):
    assert client.get(f"{ENDPOINT}?cursor=abc", headers=admin_headers).status_code == 400
    assert client.get(f"{ENDPOINT}?cursor=-5", headers=admin_headers).status_code == 400


def test_response_carries_its_caveats(client, admin_headers, f):
    f.make_job(source="s", company="Acme", title="a")

    payload = client.get(ENDPOINT, headers=admin_headers).json()
    assert payload["caveats"]
    assert any("upper bound" in c for c in payload["caveats"])


def test_blank_company_names_are_not_a_row(client, admin_headers, f):
    f.make_job(source="s", company="", title="a")

    assert not any(i["company_key"] == "" for i in _items(client, admin_headers))
