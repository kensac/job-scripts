"""A person's taste model (core.taste): fitted on what they applied to
against untouched in-scope postings, evaluated on held-out ones."""

from __future__ import annotations

import pytest

from api import db
from core import taste
from core.derivations.local_embeddings import LOCAL_EMBEDDING_DIMENSIONS

WINDOW = 30


def _vector(axis: int, wobble: int) -> str:
    values = [0.0] * LOCAL_EMBEDDING_DIMENSIONS
    values[axis] = 1.0
    values[3 + wobble % 60] += 0.05
    return "[" + ",".join(str(v) for v in values) + "]"


def _posting(f, axis: int, n: int, *, in_scope: bool = True) -> int:
    url = f"https://jobs.test/{axis}/{n}"
    job_id = f.make_job(url=url, title="Software Engineer")
    db.execute(
        "INSERT INTO posting_embeddings (url, embedding, model) VALUES (%s, %s::halfvec, 'm')",
        (url, _vector(axis, n)),
    )
    db.execute(
        "INSERT INTO posting_scopes (url, in_scope, decided_by) VALUES (%s, %s, 'title')",
        (url, in_scope),
    )
    return job_id


def _apply(user_id: int, job_id: int) -> None:
    db.execute(
        "INSERT INTO applications (user_id, job_id, company_name, title) "
        "VALUES (%s, %s, 'Acme', 'Software Engineer')",
        (user_id, job_id),
    )


@pytest.fixture
def person(f, monkeypatch):
    monkeypatch.setattr(taste, "BACKGROUND", 200)
    user_id = f.make_user()
    for n in range(30):
        _apply(user_id, _posting(f, 0, n))
    for n in range(100, 200):
        _posting(f, 1, n)
    for n in range(200, 260):
        _posting(f, 0, n)
    # Out of scope: never in the background.
    _posting(f, 2, 999, in_scope=False)
    return user_id


def test_a_model_ranks_what_the_person_applies_to_first(person):
    stored = taste.train(person, WINDOW, "m")
    assert stored["examples"]["applied"] == 30
    assert stored["examples"]["background"] == 160
    assert stored["evaluation"]["held_out_roc_auc"] > 0.5

    row = db.query_one(taste.latest_sql(), {"user_id": person})
    assert row is not None and row["id"] == stored["id"]
    scores = db.query(
        "SELECT j.url, m.intercept - (pe.embedding <#> m.weights) AS score "
        "FROM posting_embeddings pe JOIN jobs j ON j.url = pe.url, "
        f"({taste.latest_sql()}) m ORDER BY score DESC",
        {"user_id": person},
    )
    top = [r["url"] for r in scores[:20]]
    assert all("/0/" in url for url in top)


def test_too_few_applications_is_refused(f):
    user_id = f.make_user()
    with pytest.raises(ValueError, match="needs 5"):
        taste.train(user_id, WINDOW, "m")


def test_postings_marked_not_for_me_are_negatives(f, person):
    rejected = _posting(f, 0, 900)
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, %s)",
        (person, rejected, taste.NOT_FOR_ME),
    )
    applied, background = taste.examples(person, WINDOW)
    assert len(applied) == 30
    # The 160 untouched, and the one marked: touched, so not in the sample.
    assert len(background) == 161
