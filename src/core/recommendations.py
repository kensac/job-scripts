"""What the For you page reads: recent in-scope postings a person has not
touched, ordered by their taste model (core.taste), and the nearest postings to
one posting.

Every statement here works on the pool core.tech_scope scopes, which is small
(31,165 postings at the 30-day window on 2026-11-27), so ranking is an exact
scan in SQL with no vector index: one inner product a posting for the taste
order, one distance a posting for "more like this".
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

_POOL_COLUMNS = """
    j.id, j.url, j.company, j.title, j.locations,
    COALESCE(j.date_posted, j.created_at) AS posted
"""


def _pool_from(also: str) -> str:
    """The person's pool: recent in-scope postings they have not touched,
    joined with `also` (a CTE of one row the select reads)."""
    return f"""
        FROM posting_scopes s
        JOIN jobs j ON j.url = s.url
        JOIN posting_embeddings pe ON pe.url = j.url
        CROSS JOIN {also}
        WHERE s.in_scope AND {tech_scope.RECENT}
          AND j.id NOT IN ({_TOUCHED})
    """


_PAY = """
    LEFT JOIN job_comp c ON c.url = r.url
"""

_PAY_COLUMNS = "c.comp_min, c.comp_max, c.comp_period, c.comp_currency"


def ranked_sql() -> LiteralString:
    """One page of the person's pool in taste order, after the keyset
    (%(after_score)s, %(after_id)s), both NULL for the first page, with each
    row's rank and the pool's size. Params: user_id, window_days, the keyset,
    and limit."""
    return cast(
        "LiteralString",
        f"""
        WITH m AS ({taste.latest_sql()}),
        pool AS (
            SELECT {_POOL_COLUMNS}, m.intercept - (pe.embedding <#> m.weights) AS score
            {_pool_from("m")}
        ),
        r AS (
            SELECT p.*, row_number() OVER (ORDER BY p.score DESC, p.id DESC) AS rank,
                   count(*) OVER () AS total
            FROM pool p
        )
        SELECT r.*, {_PAY_COLUMNS} FROM r {_PAY}
        WHERE %(after_score)s::float8 IS NULL
           OR r.score < %(after_score)s OR (r.score = %(after_score)s AND r.id < %(after_id)s)
        ORDER BY r.score DESC, r.id DESC
        LIMIT %(limit)s
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


def copies_sql() -> LiteralString:
    """Every near copy, in the person's pool, of each of %(urls)s: its id,
    url, company, title and locations, and whether it ranks above the posting
    it copies (by the taste score, then id, the page's own order). Params:
    user_id, window_days, urls, near_copy."""
    return cast(
        "LiteralString",
        f"""
        WITH m AS ({taste.latest_sql()}),
        pool AS (
            SELECT j.id, j.url, j.company, j.title, j.locations, pe.embedding,
                   m.intercept - (pe.embedding <#> m.weights) AS score
            {_pool_from("m")}
        ),
        page AS (SELECT * FROM pool WHERE url = ANY(%(urls)s))
        SELECT page.url AS of_url, c.id, c.url, c.company, c.title, c.locations,
               (c.score, c.id) > (page.score, page.id) AS above
        FROM page JOIN pool c ON c.url <> page.url
        WHERE 1 - (c.embedding <=> page.embedding) >= %(near_copy)s
        ORDER BY page.url, c.score DESC, c.id DESC
        """,
    )


def similar_sql() -> LiteralString:
    """The person's pool nearest to the posting %(job_id)s, less its near
    copies and itself, after the keyset (%(after_distance)s, %(after_id)s),
    with the pool's size less those copies."""
    return cast(
        "LiteralString",
        f"""
        WITH target AS (
            SELECT pe.embedding FROM jobs t JOIN posting_embeddings pe ON pe.url = t.url
            WHERE t.id = %(job_id)s
        ),
        pool AS (
            SELECT {_POOL_COLUMNS}, pe.embedding <=> target.embedding AS distance
            {_pool_from("target")}
              AND j.id <> %(job_id)s
        ),
        r AS (
            SELECT p.*, count(*) OVER () AS total FROM pool p
            WHERE 1 - p.distance < %(near_copy)s
        )
        SELECT r.*, {_PAY_COLUMNS} FROM r {_PAY}
        WHERE %(after_distance)s::float8 IS NULL
           OR r.distance > %(after_distance)s
           OR (r.distance = %(after_distance)s AND r.id > %(after_id)s)
        ORDER BY r.distance, r.id
        LIMIT %(limit)s
        """,
    )
