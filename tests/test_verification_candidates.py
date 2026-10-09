from api import db, verification_candidates
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
        WITH {verification_candidates.TARGETS}
        SELECT j.id FROM jobs j
        WHERE j.active AND {verification_candidates.REACHABLE}
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


def test_gate_applies_only_enforced_managed_title_gate():
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
    db.execute(
        "UPDATE managed_boards SET title_gate = %s WHERE id = %s",
        (db.jsonb({"recipe": "internship_v1", "mode": "shadow"}), board["id"]),
    )
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
    same = {f.make_job(source=source, title="store associate") for _ in range(40)}
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


def test_a_stored_gate_with_the_removed_source_cutoff_still_loads():
    from core.review_gate import VolumeGate

    gate = VolumeGate.model_validate({"scopes": ["x"], "min_judged": 100000000})
    assert gate.scopes == ["x"] and "min_judged" not in gate.model_dump()


def test_a_source_boards_never_keep_is_skipped_except_its_audit_sample():
    _enable()
    source = f.make_source()
    _gate([_scoped_target(source)], title_min_judged=0, occupation_titles=False)
    _judged(source, 50)
    jobs = {f.make_job(source=source) for _ in range(60)}
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
