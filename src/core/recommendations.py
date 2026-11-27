"""What a person's For you ranking (api.for_you) is computed from: recent
in-scope postings they have not touched, scored by their taste model
(core.taste), and the application a posting is nearest to.

The pool is the one core.tech_scope scopes, which is small (about 58,000
postings at the 30-day window on 2026-11-27, 12,000 inside one person's
conditions), so scoring is an exact scan in SQL with no vector index: one
inner product a posting.
"""

from __future__ import annotations

from typing import LiteralString, cast

from core import taste, tech_scope

# Cosine similarity at or above which two postings are copies of one another:
# the same job reposted, in another city, or on another board. It is the
# level 95% of the catalog's known near copies reach (same source, title and
# text once location lines are removed: 3,738 pairs, 2026-11-27), where one
# random pair in a thousand passes 0.902.
NEAR_COPY = 0.922

# What a person has touched: a board row, or an application.
_TOUCHED = """
    SELECT job_id FROM user_jobs WHERE user_id = %(user_id)s
    UNION SELECT job_id FROM applications WHERE user_id = %(user_id)s AND job_id IS NOT NULL
"""


def pool_sql(gate: str, kept: str) -> LiteralString:
    """The person's pool, scored by their taste model, best first: recent
    in-scope postings they have not touched that pass `gate` (their own
    conditions on the posting `j`, composed by the caller as trusted SQL),
    each with its score, whether `kept` holds, and its embedding. Params:
    user_id, window_days, and what the gate reads."""
    return cast(
        "LiteralString",
        f"""
        WITH m AS ({taste.latest_sql()})
        SELECT j.id, m.id AS model_id, m.intercept - (pe.embedding <#> m.weights) AS score,
               ({kept}) AS kept, pe.embedding::vector::real[] AS embedding
        FROM posting_scopes s
        JOIN jobs j ON j.url = s.url
        JOIN posting_embeddings pe ON pe.url = j.url
        CROSS JOIN m
        WHERE s.in_scope AND {tech_scope.RECENT}
          AND j.id NOT IN ({_TOUCHED})
          AND ({gate})
        ORDER BY score DESC, j.id DESC
        """,
    )


def nearest_application_sql() -> LiteralString:
    """For each of %(urls)s, the person's application it is nearest to, and
    the cosine similarity between them."""
    return """
        SELECT n.url, near.job_id, near.title, near.company,
               1 - (pe.embedding <=> near.embedding) AS similarity
        FROM unnest(%(urls)s::text[]) AS n(url)
        JOIN posting_embeddings pe ON pe.url = n.url
        CROSS JOIN LATERAL (
            SELECT aj.id AS job_id, aj.title, aj.company, ape.embedding
            FROM applications a
            JOIN jobs aj ON aj.id = a.job_id
            JOIN posting_embeddings ape ON ape.url = aj.url
            WHERE a.user_id = %(user_id)s AND a.dismissed_at IS NULL
            ORDER BY pe.embedding <=> ape.embedding
            LIMIT 1
        ) near
    """
