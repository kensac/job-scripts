"""The tech scope: which recent postings are tech work.

A posting's title decides when it can (core.screening): one that names
software, data or ML work is in (software_title_v1 keeps it); one that names
other engineering, or no technical work at all, is out (technical_title_v1
skips it). A title left over names technical work only vaguely ("Engineer
II", "Member of Technical Staff", "Analyst"), and the posting's text decides:
a logistic regression on its embedding (posting_embeddings), fitted on the
two clear groups, so a vague title reads as tech when its text reads like the
software titles' text.

Measured on 272,053 active postings of 30 days (2026-11-27): 23,479 software
titles, 20,690 vague of which the classifier kept 7,686, and the rest out:
31,165 in scope, 11.5%. A classifier fitted on 15,000 of each clear group
separated held-out ones at 0.996 (cross-validated ROC AUC). The scope kept
93% of one person's applications and 94% of their board.

The model's weights are a row of ranking_models, so a posting's score is one
inner product in SQL (pgvector's <#> is the negated inner product), and its
decision is score >= 0: the classifier's own 0.5 probability.
"""

from __future__ import annotations

import logging
from typing import Any, LiteralString, cast

from core import screening
from core.pool import connection

logger = logging.getLogger(__name__)

KIND = "tech_scope"
SOFTWARE = "software_title_v1"
TECHNICAL = "technical_title_v1"

# Examples of each clear group a model is fitted on. On the study's sample,
# 15,000 of each separated held-out postings at 0.996; the fit takes under a
# minute on four cores.
EXAMPLES_PER_GROUP = 15_000

# Folds and regularisation strengths the fit chooses among by held-out ROC
# AUC (LogisticRegressionCV): the strength is measured, not picked.
_FOLDS = 4
_STRENGTHS = 6


def _skips(recipe: str, title: str) -> str:
    return screening.skips_sql(f"'{recipe}'", title=title, source="NULL::text")


def in_by_title(title: str = "j.title") -> str:
    """True when the title alone puts a posting in scope."""
    return f"(NOT {_skips(SOFTWARE, title)})"


def out_by_title(title: str = "j.title") -> str:
    """True when the title alone puts a posting out of scope."""
    return f"({_skips(SOFTWARE, title)} AND {_skips(TECHNICAL, title)})"


# Recent postings: active, available, and posted (or first seen, when the
# board gives no date) within the window. %(window_days)s is a parameter.
RECENT = """
    j.active AND j.available IS NOT FALSE
    AND COALESCE(j.date_posted, j.created_at) > now() - make_interval(days => %(window_days)s)
"""

_LATEST_MODEL = (
    "SELECT id, weights, intercept FROM ranking_models "
    f"WHERE kind = '{KIND}' AND user_id IS NULL ORDER BY id DESC LIMIT 1"
)


def score_sql() -> LiteralString:
    """Scope every recent embedded posting that has no scope, whose embedding
    is newer than its scope, or whose text-decided scope came from an older
    model, writing only rows that change. Nothing is scoped before a model
    exists. Pass screening.PARAMS and window_days."""
    title_in, title_out = in_by_title("t.title"), out_by_title("t.title")
    return cast(
        "LiteralString",
        f"""
        WITH m AS ({_LATEST_MODEL}),
        todo AS (
            SELECT j.url, j.title, pe.embedding
            FROM jobs j
            JOIN posting_embeddings pe ON pe.url = j.url
            LEFT JOIN posting_scopes s ON s.url = j.url
            CROSS JOIN m
            WHERE {RECENT}
              AND (s.url IS NULL OR pe.created_at > s.scored_at
                   OR (s.decided_by = 'text' AND s.model_id IS DISTINCT FROM m.id))
        ),
        decided AS (
            SELECT t.url,
                   CASE WHEN {title_in} THEN TRUE
                        WHEN {title_out} THEN FALSE
                        ELSE m.intercept - (t.embedding <#> m.weights) >= 0 END AS in_scope,
                   CASE WHEN {title_in} OR {title_out} THEN 'title' ELSE 'text' END AS decided_by,
                   m.id AS model_id
            FROM todo t CROSS JOIN m
        )
        INSERT INTO posting_scopes (url, in_scope, decided_by, model_id, scored_at)
        SELECT url, in_scope, decided_by,
               CASE WHEN decided_by = 'text' THEN model_id END, now()
        FROM decided
        ON CONFLICT (url) DO UPDATE SET in_scope = EXCLUDED.in_scope,
            decided_by = EXCLUDED.decided_by, model_id = EXCLUDED.model_id,
            scored_at = EXCLUDED.scored_at
        """,
    )


def score(window_days: int) -> int:
    """Scope what score_sql selects; the rows written."""
    with connection() as conn:
        cur = conn.execute(score_sql(), {**screening.PARAMS, "window_days": window_days})
        return cur.rowcount


def _examples(conn: Any, where: str, window_days: int) -> list[str]:
    sql = cast(
        "LiteralString",
        f"""
        SELECT pe.embedding::text AS embedding FROM jobs j
        JOIN posting_embeddings pe ON pe.url = j.url
        WHERE {RECENT} AND {where}
        ORDER BY random() LIMIT %(n)s
        """,
    )
    rows = conn.execute(
        sql, {**screening.PARAMS, "window_days": window_days, "n": EXAMPLES_PER_GROUP}
    ).fetchall()
    return [r["embedding"] for r in rows]


def train(window_days: int, embedding_model: str) -> dict[str, Any]:
    """Fit the scope model on recent postings whose title decides, and store
    it. Returns what was stored, less the weights."""
    import numpy as np
    from sklearn.linear_model import LogisticRegressionCV

    with connection() as conn:
        software = _examples(conn, in_by_title(), window_days)
        other = _examples(conn, out_by_title(), window_days)
    if not software or not other:
        raise ValueError("no recent postings of one of the title groups to fit the scope on")
    x = np.array(
        [np.array(e[1:-1].split(","), dtype=np.float32) for e in software + other],
        dtype=np.float32,
    )
    y = np.r_[np.ones(len(software)), np.zeros(len(other))]
    fit = LogisticRegressionCV(
        Cs=_STRENGTHS,
        cv=_FOLDS,
        scoring="roc_auc",
        class_weight="balanced",
        max_iter=3000,
    ).fit(x, y)
    held_out_auc = float(fit.scores_[1.0].mean(axis=0).max())
    weights = "[" + ",".join(repr(float(w)) for w in fit.coef_[0]) + "]"
    stored = {
        "examples": {"software": len(software), "other": len(other), "window_days": window_days},
        "evaluation": {"held_out_roc_auc": round(held_out_auc, 4), "strength": float(fit.C_[0])},
    }
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO ranking_models (kind, embedding_model, weights, intercept, examples, "
            "evaluation) VALUES (%s, %s, %s::halfvec, %s, %s, %s) RETURNING id",
            (
                KIND,
                embedding_model,
                weights,
                float(fit.intercept_[0]),
                _json(stored["examples"]),
                _json(stored["evaluation"]),
            ),
        ).fetchone()
    assert row is not None
    logger.info("tech scope model %s: %s", row["id"], stored)
    return {"id": row["id"], **stored}


def _json(value: dict[str, Any]) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)
