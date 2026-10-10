"""AI eligibility reads the working set and person state, not legacy user_jobs alone."""

from __future__ import annotations

from api import db
from core.store import AI_ELIGIBLE_JOB

# The predicate before phase 2b's cutover, kept as the reference.
OLD_AI_ELIGIBLE = AI_ELIGIBLE_JOB.replace(
    "(EXISTS (SELECT 1 FROM user_job_working_set ws WHERE ws.job_id = {job}.id)"
    " OR EXISTS (SELECT 1 FROM user_jobs uj WHERE uj.job_id = {job}.id))",
    "EXISTS (SELECT 1 FROM user_jobs uj WHERE uj.job_id = {job}.id)",
)


def _eligible(predicate: str) -> set[int]:
    rows = db.query(f"SELECT j.id FROM jobs j WHERE {predicate.format(job='j')} ORDER BY j.id")
    return {row["id"] for row in rows}


def test_working_set_and_person_rows_each_carry_an_unsubscribed_posting(f):
    assert OLD_AI_ELIGIBLE != AI_ELIGIBLE_JOB, "the reference must differ from the cutover"
    source = f.make_source()
    uid = f.make_user()
    picked = f.make_job(source=source)
    acted = f.make_job(source=source)
    neither = f.make_job(source=source)
    db.execute("INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)", (uid, picked))
    f.make_board_row(uid, acted)

    assert _eligible(AI_ELIGIBLE_JOB) == {picked, acted}
    assert neither not in _eligible(AI_ELIGIBLE_JOB)
