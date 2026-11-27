"""The tech scope (core.tech_scope): a title decides when it can, and a vague
title's text decides through a model fitted on the titles that did."""

from __future__ import annotations

import pytest

from api import db
from core import tech_scope
from core.derivations.local_embeddings import LOCAL_EMBEDDING_DIMENSIONS

WINDOW = 30


def _vector(axis: int, wobble: int) -> str:
    """A unit-ish vector along one axis, slightly different per posting."""
    values = [0.0] * LOCAL_EMBEDDING_DIMENSIONS
    values[axis] = 1.0
    values[2 + wobble % 50] += 0.05
    return "[" + ",".join(str(v) for v in values) + "]"


def _posting(f, title: str, axis: int, n: int) -> str:
    url = f"https://jobs.test/{axis}/{n}/{title}"
    f.make_job(url=url, title=title)
    db.execute(
        "INSERT INTO posting_embeddings (url, embedding, model) VALUES (%s, %s::halfvec, 'm')",
        (url, _vector(axis, n)),
    )
    return url


@pytest.fixture
def corpus(f, monkeypatch):
    # Software titles read along one axis, other work along another.
    monkeypatch.setattr(tech_scope, "EXAMPLES_PER_GROUP", 40)
    for n in range(40):
        _posting(f, "Software Engineer", 0, n)
        _posting(f, "Registered Nurse", 1, n)
    return {
        "software text": _posting(f, "Engineer II", 0, 999),
        "other text": _posting(f, "Engineer II", 1, 998),
        "by title": _posting(f, "Mechanical Engineer", 0, 997),
    }


def _scope(url: str) -> dict:
    row = db.query_one(
        "SELECT in_scope, decided_by, model_id FROM posting_scopes WHERE url = %s", (url,)
    )
    assert row is not None
    return row


def test_nothing_is_scoped_before_a_model_exists(corpus):
    assert tech_scope.score(WINDOW) == 0


def test_a_vague_title_is_decided_by_its_text(corpus):
    stored = tech_scope.train(WINDOW, "m")
    assert stored["evaluation"]["held_out_roc_auc"] > 0.5
    assert tech_scope.score(WINDOW) > 0

    assert _scope(corpus["software text"]) == {
        "in_scope": True,
        "decided_by": "text",
        "model_id": stored["id"],
    }
    assert _scope(corpus["other text"])["in_scope"] is False
    # Other engineering is out by its title, whatever its text reads like.
    assert _scope(corpus["by title"]) == {
        "in_scope": False,
        "decided_by": "title",
        "model_id": None,
    }


def test_scoring_again_writes_only_what_changed(corpus):
    tech_scope.train(WINDOW, "m")
    tech_scope.score(WINDOW)
    assert tech_scope.score(WINDOW) == 0
    # A new model re-scores only the postings whose text decided.
    tech_scope.train(WINDOW, "m")
    assert tech_scope.score(WINDOW) == 2
