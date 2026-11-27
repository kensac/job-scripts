"""A person's taste: what of the recent tech postings looks like what they
apply to.

A logistic regression on posting embeddings, fitted on the postings the
person applied to (applications with a posting) against the postings they
marked NOT_FOR_ME and a sample of recent in-scope postings they never touched
(core.tech_scope). "No Longer Interested" says nothing about taste: it meant
closed or needs sponsorship as often as not for me, which is why NOT_FOR_ME is
its own status. Until a person has marked many, the untouched sample carries
the negatives, which is positive-unlabelled learning: a posting the person
would have liked but never saw is in it, so a score orders postings rather
than judging one.

The weights are a ranking_models row, so a posting's score is one inner
product in SQL, as the scope's is.

Measured on one person (2026-11-27, 364 in-scope applications against 20,000
untouched in-scope postings): held-out ROC AUC 0.979, 90% of held-out
applications in the top 5% of in-scope postings and 64% in the top 1%. A model
fitted only on applications before 2026-06-12 put 94% of the 156 after it in
the top 5% (on the whole market, before the scope).
"""

from __future__ import annotations

import logging
from typing import Any, LiteralString, cast

from core import tech_scope
from core.pool import connection

logger = logging.getLogger(__name__)

KIND = "taste"

# The board status a person gives a posting they decided is not for them: the
# taste model's one true negative. A status, so it is served and written like
# every other (api.routers.job_board.DEFAULT_STATUSES).
NOT_FOR_ME = "Not For Me"

# Untouched in-scope postings a model is fitted against: the study's sample,
# at which held-out applications ranked as above; more takes longer to fit and
# moved nothing there.
BACKGROUND = 20_000

# Held-out folds the evaluation stores, and the folds and regularisation
# strengths the fit chooses among by held-out ROC AUC (LogisticRegressionCV).
_EVALUATION_FOLDS = 5
_FIT_FOLDS = 4
_STRENGTHS = 10

# The share of postings a held-out application must rank inside to count in
# the evaluation's headline: the top 5% of in-scope postings is about the
# first screen and a half of the For you page at the study's 31,000.
TOP_SHARE = 0.05

_APPLIED = """
    SELECT DISTINCT a.job_id FROM applications a
    WHERE a.user_id = %(user_id)s AND a.job_id IS NOT NULL AND a.dismissed_at IS NULL
"""

_TOUCHED = """
    SELECT job_id FROM user_jobs WHERE user_id = %(user_id)s
    UNION SELECT job_id FROM applications WHERE user_id = %(user_id)s AND job_id IS NOT NULL
"""


def _vectors(conn: Any, sql: str, params: dict[str, Any]) -> list[list[float]]:
    return [r["v"] for r in conn.execute(cast("LiteralString", sql), params).fetchall()]


def examples(user_id: int, window_days: int) -> tuple[list[list[float]], list[list[float]]]:
    """The person's applied postings' embeddings, and the negatives: the
    postings they marked NOT_FOR_ME and a sample of recent in-scope postings
    they never touched."""
    params = {
        "user_id": user_id,
        "window_days": window_days,
        "n": BACKGROUND,
        "not_for_me": NOT_FOR_ME,
    }
    with connection() as conn:
        applied = _vectors(
            conn,
            f"""
            SELECT pe.embedding::vector::real[] AS v FROM jobs j
            JOIN posting_embeddings pe ON pe.url = j.url
            WHERE j.id IN ({_APPLIED})
            """,
            params,
        )
        background = _vectors(
            conn,
            f"""
            SELECT pe.embedding::vector::real[] AS v FROM jobs j
            JOIN posting_embeddings pe ON pe.url = j.url
            JOIN posting_scopes s ON s.url = j.url AND s.in_scope
            WHERE {tech_scope.RECENT} AND j.id NOT IN ({_TOUCHED})
            ORDER BY random() LIMIT %(n)s
            """,
            params,
        )
        background += _vectors(
            conn,
            f"""
            SELECT pe.embedding::vector::real[] AS v FROM user_jobs uj
            JOIN jobs j ON j.id = uj.job_id
            JOIN posting_embeddings pe ON pe.url = j.url
            WHERE uj.user_id = %(user_id)s AND uj.status = %(not_for_me)s
              AND uj.job_id NOT IN ({_APPLIED})
            """,
            params,
        )
    return applied, background


def _fit(x: Any, y: Any) -> Any:
    from sklearn.linear_model import LogisticRegressionCV

    return LogisticRegressionCV(
        Cs=_STRENGTHS, cv=_FIT_FOLDS, scoring="roc_auc", class_weight="balanced", max_iter=4000
    ).fit(x, y)


def evaluate(applied: Any, background: Any) -> dict[str, Any]:
    """Held-out applications against held-out background, over folds: the
    ROC AUC, and the share of held-out applications that rank inside
    TOP_SHARE of the held-out background."""
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import KFold

    aucs: list[float] = []
    ranks: list[float] = []
    folds = KFold(_EVALUATION_FOLDS, shuffle=True)
    for (train_a, test_a), (train_b, test_b) in zip(
        folds.split(applied), folds.split(background), strict=True
    ):
        fit = _fit(
            np.vstack([applied[train_a], background[train_b]]),
            np.r_[np.ones(len(train_a)), np.zeros(len(train_b))],
        )
        held_a = fit.decision_function(applied[test_a])
        held_b = np.sort(fit.decision_function(background[test_b]))
        aucs.append(
            float(
                roc_auc_score(
                    np.r_[np.ones(len(held_a)), np.zeros(len(held_b))], np.r_[held_a, held_b]
                )
            )
        )
        ranks.extend(1 - np.searchsorted(held_b, held_a) / len(held_b))
    return {
        "held_out_roc_auc": round(float(np.mean(aucs)), 4),
        "held_out_in_top_share": round(float(np.mean(np.array(ranks) <= TOP_SHARE)), 4),
        "top_share": TOP_SHARE,
        "folds": _EVALUATION_FOLDS,
    }


def train(user_id: int, window_days: int, embedding_model: str) -> dict[str, Any]:
    """Fit and store a person's taste model; what was stored, less weights."""
    import numpy as np
    from psycopg.types.json import Jsonb

    applied_rows, background_rows = examples(user_id, window_days)
    if len(applied_rows) < _EVALUATION_FOLDS or len(background_rows) < _EVALUATION_FOLDS:
        raise ValueError(
            f"{len(applied_rows)} applied and {len(background_rows)} untouched in-scope postings "
            f"with embeddings: a model needs {_EVALUATION_FOLDS} of each to be evaluated"
        )
    applied = np.array(applied_rows, dtype=np.float32)
    background = np.array(background_rows, dtype=np.float32)
    evaluation = evaluate(applied, background)
    fit = _fit(
        np.vstack([applied, background]),
        np.r_[np.ones(len(applied)), np.zeros(len(background))],
    )
    evaluation["strength"] = float(fit.C_[0])
    stored_examples = {
        "applied": len(applied),
        "background": len(background),
        "window_days": window_days,
    }
    weights = "[" + ",".join(repr(float(w)) for w in fit.coef_[0]) + "]"
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO ranking_models (kind, user_id, embedding_model, weights, intercept, "
            "examples, evaluation) VALUES (%s, %s, %s, %s::halfvec, %s, %s, %s) RETURNING id",
            (
                KIND,
                user_id,
                embedding_model,
                weights,
                float(fit.intercept_[0]),
                Jsonb(stored_examples),
                Jsonb(evaluation),
            ),
        ).fetchone()
    assert row is not None
    logger.info("taste model %s for user %s: %s", row["id"], user_id, evaluation)
    return {"id": row["id"], "examples": stored_examples, "evaluation": evaluation}


def latest_sql() -> LiteralString:
    """The person's latest taste model (id, weights, intercept, evaluation,
    examples, trained_at), for %(user_id)s."""
    return (
        "SELECT id, weights, intercept, evaluation, examples, trained_at FROM ranking_models "
        f"WHERE kind = '{KIND}' AND user_id = %(user_id)s ORDER BY id DESC LIMIT 1"
    )
