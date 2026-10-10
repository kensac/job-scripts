from api import db, verification_candidates
from api.board import criteria
from api.locations import LocationExtract
from api.locations import store as store_location
from core import catalog, verdict_reads
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL, ON_A_BOARD
from tasks import verify as tasks_verify
from tests import factories as f


def test_disabled_gate_preserves_subscribed_jobs_without_an_active_filter():
    source = f.make_source()
    user = f.make_user()
    f.subscribe(user, source)
    job = f.make_job(source=source)
    assert _reachable() == {job}
    _enable()
    assert _reachable() == set()


def test_another_personal_target_can_admit_a_job_outside_one_users_window():
    _enable()
    source = f.make_source()
    _paid_personal_target(source, {"max_age_days": 7})
    _paid_personal_target(source, {"max_age_days": 30})
    job = f.make_job(source=source)
    db.execute("UPDATE jobs SET date_posted = current_date - 14 WHERE id = %s", (job,))
    assert _reachable() == {job}


def _reachable() -> set[int]:
    rows = db.query(
        f"""
        WITH {verification_candidates.TARGETS},
        pool AS (
            SELECT j.id, ({verification_candidates.EVERY_TARGET}) AS every_target
            FROM jobs j WHERE j.active
        )
        SELECT id FROM ({verification_candidates.reachable("pool")}) r
        """,
        verification_candidates.params(),
    )
    return {row["id"] for row in rows}


def _enable() -> None:
    db.execute(
        "UPDATE app_config SET value = 'true' WHERE key = 'verification_reachability_gate_enabled'"
    )


def _paid_personal_target(source: str, criteria: dict) -> int:
    user_id = f.make_user()
    f.subscribe(user_id, source)
    f.make_filter(user_id)
    db.execute(
        "INSERT INTO user_settings (user_id, api_key_enc, criteria) VALUES (%s, %s, %s)",
        (user_id, b"paid", db.jsonb(criteria)),
    )
    return user_id


def test_gate_uses_personal_date_and_terms_before_verification():
    _enable()
    source = f.make_source()
    _paid_personal_target(source, {"max_age_days": 7, "included_terms": ["Full-time"]})
    kept = f.make_job(source=source)
    wrong_term = f.make_job(source=source)
    old = f.make_job(source=source)
    db.execute("UPDATE jobs SET terms = ARRAY['Full-time'] WHERE id = %s", (kept,))
    db.execute("UPDATE jobs SET terms = ARRAY['Internship'] WHERE id = %s", (wrong_term,))
    db.execute(
        "UPDATE jobs SET terms = ARRAY['Full-time'], date_posted = current_date - 8 WHERE id = %s",
        (old,),
    )

    assert _reachable() == {kept}


def test_gate_keeps_tracked_job_outside_current_filter_criteria():
    _enable()
    source = f.make_source()
    user_id = _paid_personal_target(source, {"max_age_days": 7})
    old = f.make_job(source=source)
    db.execute("UPDATE jobs SET date_posted = current_date - 8 WHERE id = %s", (old,))
    f.make_board_row(user_id, old)

    assert _reachable() == {old}


def test_gate_applies_a_managed_title_gate():
    _enable()
    source = f.make_source()
    sponsor = f.make_user()
    board = db.query_one(
        """
        INSERT INTO managed_boards
            (slug, name, sponsor_user_id, prompt, prompt_hash, requested_model,
             title_gate, published, public_revision, published_at)
        VALUES ('interns', 'Interns', %s, 'internships', 'hash', 'gpt-5-mini',
                %s, TRUE, 1, now())
        RETURNING id
        """,
        (sponsor, db.jsonb({"recipe": "internship_v1", "mode": "enforce"})),
    )
    db.execute(
        "INSERT INTO managed_board_sources (managed_board_id, source) VALUES (%s, %s)",
        (board["id"], source),
    )
    internship = f.make_job(source=source, title="Software Engineering Intern")
    full_time = f.make_job(source=source, title="Software Engineer")

    assert _reachable() == {internship}
    db.execute("UPDATE managed_boards SET title_gate = NULL WHERE id = %s", (board["id"],))
    assert _reachable() == {internship, full_time}


def _gate(scopes: list[str], **overrides) -> None:
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('verification_volume_gate', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb({"scopes": scopes, **overrides}),),
    )


def _scoped_target(source: str) -> str:
    _paid_personal_target(source, {})
    return db.query_one("SELECT prompt_hash FROM user_filters ORDER BY id DESC LIMIT 1")[
        "prompt_hash"
    ]


def _judged(source: str, n: int, status: str = "rejected") -> None:
    for _ in range(n):
        job = f.make_job(source=source)
        url = db.query_one("SELECT url FROM jobs WHERE id = %s", (job,))["url"]
        f.make_verdict(url, "custom", status)
        db.execute("UPDATE jobs SET active = false WHERE id = %s", (job,))


def _audited(url: str) -> bool:
    return db.query_one("SELECT abs(hashtext(%s)) %% 100 < 5 AS a", (url,))["a"]


def test_an_unlisted_target_and_a_tracked_posting_read_everything():
    _enable()
    source = f.make_source()
    user_id = _paid_personal_target(source, {})
    _gate(["some-other-prompt"])
    _judged(source, 50)
    job = f.make_job(source=source)
    assert _reachable() == {job}

    _gate(
        [
            db.query_one("SELECT prompt_hash FROM user_filters WHERE user_id = %s", (user_id,))[
                "prompt_hash"
            ]
        ]
    )
    nurse = f.make_job(source=f.make_source(), title="Registered Nurse")
    f.make_board_row(user_id, nurse)
    assert nurse in _reachable(), "a posting someone tracks is always read"


def test_occupation_titles_are_skipped_unless_a_technical_word_wins():
    _enable()
    source = f.make_source()
    _gate([_scoped_target(source)])
    nurse = f.make_job(source=source, title="Registered Nurse - ICU Nights")
    cook = f.make_job(source=source, title="Line Cook")
    informatics = f.make_job(source=source, title="Nurse Informatics Software Engineer")
    nursery = f.make_job(source=source, title="Nursery Associate")

    assert _reachable() == {informatics, nursery}
    _gate([_scoped_target(source)], occupation_titles=False)
    assert {nurse, cook} <= _reachable()


def test_a_title_judged_often_with_no_keep_is_skipped_except_its_audit_sample():
    _enable()
    source = f.make_source()
    _gate(
        [_scoped_target(source)], title_min_judged=50, occupation_titles=False, source_min_judged=0
    )
    for _ in range(50):
        job = f.make_job(source=source, title="Store  Associate")
        url = db.query_one("SELECT url FROM jobs WHERE id = %s", (job,))["url"]
        f.make_verdict(url, "custom", "rejected")
        db.execute("UPDATE jobs SET active = false WHERE id = %s", (job,))
    # Named urls: the factory's counter runs across the whole session, so its
    # urls, and which of them hash into the audit sample, depend on test order.
    same = {
        f.make_job(source=source, title="store associate", url=f"https://jobs.test/title-audit-{i}")
        for i in range(40)
    }
    other = f.make_job(source=source, title="Software Engineer")
    urls = {
        r["id"]: r["url"]
        for r in db.query("SELECT id, url FROM jobs WHERE id = ANY(%s)", (list(same),))
    }

    reached = _reachable()

    assert other in reached
    assert reached & same == {job for job in same if _audited(urls[job])}


def test_a_title_with_one_keep_is_read():
    _enable()
    source = f.make_source()
    _gate(
        [_scoped_target(source)], title_min_judged=50, occupation_titles=False, source_min_judged=0
    )
    for status in ["rejected"] * 49 + ["passed"]:
        job = f.make_job(source=source, title="Data Analyst")
        url = db.query_one("SELECT url FROM jobs WHERE id = %s", (job,))["url"]
        f.make_verdict(url, "custom", status)
        db.execute("UPDATE jobs SET active = false WHERE id = %s", (job,))
    job = f.make_job(source=source, title="Data Analyst")

    assert job in _reachable()


def test_a_source_boards_never_keep_is_skipped_except_its_audit_sample():
    _enable()
    source = f.make_source()
    _gate([_scoped_target(source)], title_min_judged=0, occupation_titles=False)
    _judged(source, 50)
    # Named urls, five of which hash into the 5% sample. With the factory's
    # session-wide counter the sample depended on test order, and about one
    # order in twenty drew none of 60 and failed the second assertion.
    jobs = {f.make_job(source=source, url=f"https://jobs.test/source-audit-{i}") for i in range(60)}
    urls = {
        r["id"]: r["url"]
        for r in db.query("SELECT id, url FROM jobs WHERE id = ANY(%s)", (list(jobs),))
    }

    reached = _reachable()

    assert reached == {job for job in jobs if _audited(urls[job])}
    assert reached, "the audit sample keeps reading the source"


def test_a_source_over_the_keep_rate_is_read():
    _enable()
    source = f.make_source()
    _gate(
        [_scoped_target(source)],
        title_min_judged=0,
        occupation_titles=False,
        source_max_keep_rate=0.01,
    )
    _judged(source, 98)
    _judged(source, 2, "passed")
    job = f.make_job(source=source)

    assert job in _reachable(), "2 keeps in 100 is above a 1% rate"


def test_a_title_the_review_gate_screens_is_not_read_for_its_prompt():
    _enable()
    source = f.make_source()
    prompt_hash = _scoped_target(source)
    nurse = f.make_job(source=source, title="Registered Nurse")
    engineer = f.make_job(source=source, title="Software Engineer")

    _title_screens({prompt_hash: "nontechnical_occupations_v1"})
    assert _reachable() == {engineer}
    _title_screens({})
    assert _reachable() == {nurse, engineer}


def _title_screens(value: dict) -> None:
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('title_screens', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb(value),),
    )


# The verify sweep's candidate query as it stood before it was restructured
# around verification_candidates.reachable(): a correlated EXISTS per posting
# under an OR, and the page text read for every eligible posting before the
# sort. It ran past 14 minutes on production. Kept as the oracle the new query
# must agree with, built from the same fragments so only the shape differs.
_OLD_REACHABLE = f"""
(
    (NOT %(verification_reachability_gate_enabled)s AND {AI_ELIGIBLE_JOB.format(job="j")})
    OR (%(verification_reachability_gate_enabled)s AND (
    NOT EXISTS (SELECT 1 FROM sources source WHERE source.name = j.source)
    OR {ON_A_BOARD.format(job="j")}
    OR EXISTS (
        SELECT 1 FROM verification_targets target
        WHERE target.source = j.source
        {criteria.json_sql("target.criteria")}
        {verification_candidates._TITLE_SQL}
        {verification_candidates._VOLUME_SKIP}
    )))
)
"""

_OLD_CANDIDATES = f"""
    WITH {verification_candidates.TARGETS}, candidates AS (
    SELECT j.url, j.source, j.company, j.title, q.input_content,
           q.id AS page_fetch_id,
           NOT {verdict_reads.has_verdict("j.url", "closed")} AS needs_closed,
           NOT {verdict_reads.has_verdict("j.url", "clearance")} AS needs_clearance
    FROM jobs j
    {CONTENT_LATERAL.format(url="j.url", columns="id, input_content")}
    WHERE {catalog.IS_AVAILABLE.format(job="j")} AND {_OLD_REACHABLE}
      AND NOT (j.url = ANY(%(in_flight)s::text[])) AND (
        NOT {verdict_reads.has_verdict("j.url", "closed")}
        OR NOT {verdict_reads.has_verdict("j.url", "clearance")}
    )
    ORDER BY j.date_posted DESC NULLS LAST
    LIMIT %(cap)s
    )
    SELECT * FROM candidates
"""


def _posting(
    source: str, title: str, days_ago: int | None, places: list[str] | None = None, **verdicts: str
) -> tuple[int, str]:
    job, url = f.make_ready_job(
        source=source, title=title, **{"closed": "", "clearance": "", **verdicts}
    )
    db.execute(
        "UPDATE jobs SET date_posted = current_date - %s::int, locations = %s WHERE id = %s",
        (days_ago, places or [], job),
    )
    return job, url


def test_the_restructured_sweep_selects_exactly_what_the_old_one_did():
    """Every arm of the gate, and every filter in front of it, on one catalog:
    the new candidate query returns the old one's rows, in its order, with the
    gate on and off and with a cap that cuts the list. Two people read one
    source with different places, so a posting one refuses the other admits,
    and postings share location lists, which the new query checks once."""
    for text, place in {
        "United States": LocationExtract(country="US"),
        "Austin, TX": LocationExtract(country="US", region="TX"),
        "Canada": LocationExtract(country="CA"),
        "Toronto": LocationExtract(country="CA", city="Toronto"),
        "London": LocationExtract(country="GB", city="London"),
    }.items():
        store_location(text, place, "t")
    subscribed, board_source, lonely = f.make_source(), f.make_source(), f.make_source()
    user_id = _paid_personal_target(
        subscribed,
        {
            "max_age_days": 30,
            "included_locations": ["United States"],
            "excluded_locations": ["Austin, TX"],
        },
    )
    # Another prompt, outside the volume gate's scopes: it reads the nurse's
    # title, and its places refuse the nurse's location.
    other = f.make_user()
    f.subscribe(other, subscribed)
    f.make_filter(other, prompt="must be a data role")
    db.execute(
        "INSERT INTO user_settings (user_id, api_key_enc, criteria) VALUES (%s, %s, %s)",
        (other, b"paid", db.jsonb({"max_age_days": 30, "included_locations": ["Canada"]})),
    )
    sponsor = f.make_user()
    board = db.query_one(
        """
        INSERT INTO managed_boards
            (slug, name, sponsor_user_id, prompt, prompt_hash, requested_model,
             title_gate, criteria, published, public_revision, published_at)
        VALUES ('interns', 'Interns', %s, 'internships', 'hash', 'gpt-5-mini',
                %s, %s, TRUE, 1, now())
        RETURNING id
        """,
        (
            sponsor,
            db.jsonb({"recipe": "internship_v1", "mode": "enforce"}),
            db.jsonb({"included_locations": ["Canada"]}),
        ),
    )
    db.execute(
        "INSERT INTO managed_board_sources (managed_board_id, source) VALUES (%s, %s)",
        (board["id"], board_source),
    )
    personal_prompt = db.query_one(
        "SELECT prompt_hash FROM user_filters WHERE user_id = %s", (user_id,)
    )["prompt_hash"]
    _gate([personal_prompt])

    us = ["United States"]
    _posting(subscribed, "personal target match", 1, us)
    _posting(subscribed, "Registered Nurse", 2, us)
    _posting(subscribed, "closed answered", 3, us, closed="passed")
    _posting(subscribed, "both answered", 4, us, closed="passed", clearance="passed")
    _, in_flight = _posting(subscribed, "in flight", 5, us)
    inactive, _ = _posting(subscribed, "inactive", 6, us)
    db.execute("UPDATE jobs SET active = false WHERE id = %s", (inactive,))
    no_text = f.make_job(source=subscribed, title="no page text")
    db.execute("UPDATE jobs SET date_posted = current_date - 7 WHERE id = %s", (no_text,))
    _posting(board_source, "Software Engineering Intern", 8)
    _posting(board_source, "Staff Software Engineer", 9)
    _posting("source-with-no-row", "no sources row", 10)
    tracked, _ = _posting(lonely, "tracked", 11)
    f.make_board_row(user_id, tracked)
    working, _ = _posting(lonely, "working set", 12)
    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)", (user_id, working)
    )
    _posting(lonely, "nobody reads", 13)
    _posting(subscribed, "in Austin", 14, ["Austin, TX"])
    _posting(subscribed, "in Toronto", 15, ["Toronto"])
    _posting(subscribed, "in London", 16, ["London"])
    _posting(subscribed, "no location", 17)
    _posting(subscribed, "London and Austin", 18, ["London", "Austin, TX"])
    _posting(subscribed, "same places as the first", 19, us)
    _posting(board_source, "Software Engineering Intern London", 20, ["London"])
    _posting(board_source, "Software Engineering Intern Toronto", 21, ["Toronto"])
    _posting(subscribed, "outside the personal window", 60, us)
    _posting(subscribed, "no date", None, us)

    expected = {
        True: [
            "personal target match",
            "closed answered",
            "Software Engineering Intern",
            "no sources row",
            "tracked",
            "working set",
            "in Toronto",
            "no location",
            "same places as the first",
            "Software Engineering Intern Toronto",
            "no date",
        ],
        False: [
            "personal target match",
            "Registered Nurse",
            "closed answered",
            "no sources row",
            "tracked",
            "working set",
            "in Austin",
            "in Toronto",
            "in London",
            "no location",
            "London and Austin",
            "same places as the first",
            "outside the personal window",
            "no date",
        ],
    }
    for gate in (True, False):
        db.execute(
            "UPDATE app_config SET value = %s WHERE key = 'verification_reachability_gate_enabled'",
            (db.jsonb(gate),),
        )
        for cap in (100, 3):
            params = {**verification_candidates.params(), "cap": cap, "in_flight": [in_flight]}
            old = db.query(_OLD_CANDIDATES, params)
            new = db.query(tasks_verify.candidates_sql(), params)
            assert new == old, (gate, cap)
            assert [row["title"] for row in new] == expected[gate][:cap], (gate, cap)
