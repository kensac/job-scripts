from __future__ import annotations

import datetime

from api import db, signals


def _detail(client, headers, job_id: int) -> dict:
    resp = client.get(f"/v1/user/jobs/{job_id}/detail", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _own(f, user_id: int, **kwargs) -> tuple[int, str]:
    """A job on the user's own board, so it is visible without needing a
    subscription, filters or a passing clearance check."""
    job_id, url = f.make_ready_job(**kwargs)
    f.make_board_row(user_id, job_id)
    return job_id, url


def _set_posted(job_id: int, days_ago: int) -> None:
    db.execute(
        "UPDATE jobs SET date_posted = now() - make_interval(days => %s) WHERE id = %s",
        (days_ago, job_id),
    )


def _clear_board_cache() -> None:
    signals._board_cache.clear()


# --- absence is the contract -------------------------------------------------


def test_signals_that_cannot_clear_their_floor_are_omitted_not_nulled(client, user_headers, f):
    """The whole design: a missing key means the signal does not exist. A null,
    a zero or a reason string would all invite the caller to render it."""
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="thin-board")
    db.execute("UPDATE jobs SET date_posted = NULL WHERE id = %s", (job_id,))

    payload = _detail(client, user_headers, job_id)
    assert payload["signals"] == {}


def test_posting_age_is_absent_when_the_board_supplied_no_date(client, user_headers, f):
    """sheet_import carries date_posted for 1,628 of 6,021 postings and upload
    for none. The only fallback would be created_at, which is a catalog-load
    timestamp, so absence is correct."""
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="undated")
    db.execute("UPDATE jobs SET date_posted = NULL WHERE id = %s", (job_id,))

    assert "posting_age" not in _detail(client, user_headers, job_id)["signals"]


def test_posting_age_is_days_since_date_posted(client, user_headers, f):
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="dated")
    _set_posted(job_id, 94)

    age = _detail(client, user_headers, job_id)["signals"]["posting_age"]
    assert age["days_listed"] == 94
    assert age["posted_at"] is not None


def test_a_future_date_posted_is_treated_as_a_feed_error(client, user_headers, f):
    """A posting from tomorrow is a bad row, not a negative age."""
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="futured")
    _set_posted(job_id, -5)

    assert "posting_age" not in _detail(client, user_headers, job_id)["signals"]


# --- board reliability -------------------------------------------------------


def test_board_reliability_needs_its_sample_floor(client, user_headers, f):
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="tiny-board")

    assert "board_reliability" not in _detail(client, user_headers, job_id)["signals"]


def test_board_reliability_counts_first_check_rejections(client, user_headers, f):
    """Dead on arrival is the FIRST closed-check, not the latest. A posting
    that arrived alive and closed later is not dead on arrival."""
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    for i in range(signals.BOARD_RELIABILITY_MIN_CHECKED):
        f.make_ready_job(source="wide-board", closed="rejected" if i < 4 else "passed")
    # Arrived alive, closed since: counts in the denominator, not the numerator.
    _, later = f.make_ready_job(source="wide-board", closed="passed")
    f.make_verdict(later, "closed", "rejected")
    job_id, _ = _own(f, user_id["id"], source="wide-board")

    signal = _detail(client, user_headers, job_id)["signals"]["board_reliability"]
    assert signal["source"] == "wide-board"
    assert signal["dead_on_arrival"] == 4
    assert signal["sample_n"] == signals.BOARD_RELIABILITY_MIN_CHECKED + 2


def test_board_reliability_is_cached_per_source(client, user_headers, f):
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    for _ in range(signals.BOARD_RELIABILITY_MIN_CHECKED):
        f.make_ready_job(source="cached-board", closed="passed")
    job_id, _ = _own(f, user_id["id"], source="cached-board")

    first = _detail(client, user_headers, job_id)["signals"]["board_reliability"]
    # A verdict landing after the fill is not reflected until the TTL lapses,
    # which is the trade the cache exists to make.
    f.make_ready_job(source="cached-board", closed="rejected")
    assert _detail(client, user_headers, job_id)["signals"]["board_reliability"] == first
    _clear_board_cache()
    assert _detail(client, user_headers, job_id)["signals"]["board_reliability"] != first


# --- repost ------------------------------------------------------------------


def test_repost_ignores_the_same_role_seen_on_another_board(client, user_headers, f):
    """One posting syndicated to two boards is our ingest seeing it twice, not
    an employer re-listing it."""
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="board-x", company="Acme", title="Engineer")
    other = f.make_job(source="board-y", company="Acme", title="Engineer")
    _set_posted(job_id, 100)
    _set_posted(other, 10)

    assert "repost" not in _detail(client, user_headers, job_id)["signals"]


def test_repost_ignores_same_day_duplicates(client, user_headers, f):
    """38% of same-source duplicate groups share a single day, and differing
    locations do not explain them. Whatever they are, they are not reposts."""
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="board-z", company="Acme", title="Engineer")
    twin = f.make_job(source="board-z", company="Acme", title="Engineer")
    _set_posted(job_id, 30)
    _set_posted(twin, 30)

    assert "repost" not in _detail(client, user_headers, job_id)["signals"]


def test_repost_reports_a_role_relisted_on_the_same_board_over_time(client, user_headers, f):
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="board-r", company="Acme", title="Engineer")
    again = f.make_job(source="board-r", company="Acme", title="Engineer")
    _set_posted(job_id, 90)
    _set_posted(again, 10)

    signal = _detail(client, user_headers, job_id)["signals"]["repost"]
    assert signal["url_count"] == 2
    assert signal["span_days"] == 80
    assert signal["title"] == "Engineer"


def test_repost_excludes_one_role_listed_across_many_locations(client, user_headers, f):
    """A chain listing one role across its estate is bulk hiring, not
    re-listing. Without location in the key, Sainsbury's "Trading Assistant"
    grouped to 1,056 urls over 120 locations and rendered as a repost."""
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="chain", company="Grocer", title="Assistant")
    other_store = f.make_job(source="chain", company="Grocer", title="Assistant")
    db.execute("UPDATE jobs SET locations = %s WHERE id = %s", (["Leeds, UK"], job_id))
    db.execute("UPDATE jobs SET locations = %s WHERE id = %s", (["Bath, UK"], other_store))
    _set_posted(job_id, 90)
    _set_posted(other_store, 10)

    assert "repost" not in _detail(client, user_headers, job_id)["signals"]


def test_repost_counts_the_same_role_relisted_at_one_location(client, user_headers, f):
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="chain2", company="Grocer", title="Assistant")
    again = f.make_job(source="chain2", company="Grocer", title="Assistant")
    db.execute(
        "UPDATE jobs SET locations = %s WHERE id IN (%s, %s)", (["Leeds, UK"], job_id, again)
    )
    _set_posted(job_id, 90)
    _set_posted(again, 10)

    assert _detail(client, user_headers, job_id)["signals"]["repost"]["url_count"] == 2


def test_repost_matches_on_casefolded_company_and_title(client, user_headers, f):
    """The match is text, not identity - which is exactly why a caller may say
    the name recurred and not that the employer reposts."""
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="board-c", company="Acme", title="Engineer")
    variant = f.make_job(source="board-c", company="  ACME ", title="engineer")
    _set_posted(job_id, 90)
    _set_posted(variant, 10)

    assert _detail(client, user_headers, job_id)["signals"]["repost"]["url_count"] == 2


# --- closed_verdict on the board row ----------------------------------------


def test_closed_verdict_distinguishes_never_checked_from_closed(client, user_headers, f):
    """The bug this column exists to fix: `active` reports our stale copy of a
    board's listing as the posting's state, so an unchecked posting and a dead
    one were rendered identically."""
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    uid = user_id["id"]
    open_id, _ = _own(f, uid, source="verdicts", closed="passed")
    closed_id, _ = _own(f, uid, source="verdicts", closed="rejected")
    unchecked = f.make_job(source="verdicts", active=False)
    f.make_board_row(uid, unchecked)

    rows = client.get("/v1/user/jobs?limit=100", headers=user_headers).json()["rows"]
    verdict = {r["job_id"]: r["closed_verdict"] for r in rows}
    assert verdict[open_id] == "open"
    assert verdict[closed_id] == "closed"
    assert verdict[unchecked] is None


def test_closed_verdict_can_contradict_the_stale_active_flag(client, user_headers, f):
    """114 of the applications the board flags dead via `active` have a
    closed-check saying the posting is open. Both values are served so the
    caller can stop trusting the wrong one."""
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="stale-feed", closed="passed", active=False)

    row = next(
        r
        for r in client.get("/v1/user/jobs?limit=100", headers=user_headers).json()["rows"]
        if r["job_id"] == job_id
    )
    assert row["active"] is False
    assert row["closed_verdict"] == "open"


def test_detail_route_also_carries_the_closed_verdict(client, user_headers, f):
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="detailed", closed="rejected")

    assert _detail(client, user_headers, job_id)["job"]["closed_verdict"] == "closed"


def test_signals_are_scoped_to_a_job_the_user_may_see(client, user_headers, f):
    """signals hangs off the detail route so it inherits _require_visible_job
    rather than re-implementing the gate."""
    stranger = f.make_user()
    hidden = f.make_job(source="private", uploaded_by=stranger)

    resp = client.get(f"/v1/user/jobs/{hidden}/detail", headers=user_headers)
    assert resp.status_code == 404


def test_signal_timestamps_are_timezone_aware(client, user_headers, f):
    _clear_board_cache()
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = _own(f, user_id["id"], source="tz")
    _set_posted(job_id, 30)

    posted = _detail(client, user_headers, job_id)["signals"]["posting_age"]["posted_at"]
    assert datetime.datetime.fromisoformat(posted).tzinfo is not None
