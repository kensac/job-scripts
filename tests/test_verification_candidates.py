from api import db, verification_candidates
from tests import factories as f


def _reachable() -> set[int]:
    rows = db.query(
        f"""
        WITH {verification_candidates.TARGETS}
        SELECT j.id FROM jobs j
        WHERE j.active AND {verification_candidates.LEGACY_REACHABLE}
          AND {verification_candidates.REACHABLE}
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
