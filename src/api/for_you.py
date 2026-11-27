"""A person's For you ranking, computed in a task and read by the page.

The ranking is the person's recent tech postings they have not touched,
scored by their taste model (core.taste), held to their board's conditions
less where a posting is listed (GATE), with a job's copies folded into one
row. Evaluated on each read that cost 6.8 s a page and 6.6 s for its copies
(production, warm, 2026-11-27), so `compute` runs it in a task
(tasks.for_you) and stores the result in recommended_postings, as board
membership is computed into board_visible (docs/agents/visibility.md); the
routes read it.
"""

from __future__ import annotations

from typing import Any

from api import db
from api.board import criteria, eligibility
from core import catalog, recommendations, verdict_reads

# The latest answer of one of the person's enabled filters (`ef`) for `j`.
_FILTER_ANSWER = verdict_reads.latest("j.url", "custom", prompt_hash="ef.prompt_hash")
_ENABLED = eligibility.ENABLED_FILTERS

# What the person's board holds a posting to, less where it is listed: their
# criteria (places, posted date), and no answer against it from the shared
# closed and clearance checks (clearance unless they bypass it) or from one of
# their enabled filters. A posting none of these has looked at yet stays: the
# pool reaches past the sources the person subscribes to, which is where
# their filter's work stops.
GATE = f"""TRUE {criteria.SQL}
    AND {verdict_reads.posting_status("j", "closed")} IS DISTINCT FROM 'rejected'
    AND (%(bypass_sponsorship)s
         OR {verdict_reads.posting_status("j", "clearance")} IS DISTINCT FROM 'rejected')
    AND NOT EXISTS (SELECT 1 FROM ({_ENABLED}) ef WHERE ({_FILTER_ANSWER}) = 'rejected')"""

# Every enabled filter passed it, and the person has one.
KEPT = f"""(EXISTS (SELECT 1 FROM ({_ENABLED}) ef)
    AND NOT EXISTS (SELECT 1 FROM ({_ENABLED}) ef
                    WHERE ({_FILTER_ANSWER}) IS DISTINCT FROM 'passed'))"""

# Rows the near-copy fold compares at once: a block's similarities to the
# whole pool are BLOCK x pool float32 values, 50 MB at 12,000 postings.
_BLOCK = 1024


def params(user_id: int) -> dict[str, Any]:
    return {**eligibility.settings_params(user_id), "user_id": user_id}


def has_filter(user_id: int) -> bool:
    return db.query_one(f"SELECT 1 FROM ({_ENABLED}) ef", params(user_id)) is not None


def _fold(vectors: Any) -> list[int | None]:
    """For each posting in rank order, the index of the posting above it it
    is a near copy of (core.recommendations.NEAR_COPY), or None: the first
    such posting that is not itself folded, so each job is one row."""
    import numpy as np

    n = len(vectors)
    unit = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    copy_of: list[int | None] = [None] * n
    for start in range(0, n, _BLOCK):
        sims = unit[start : start + _BLOCK] @ unit.T
        for row in range(sims.shape[0]):
            i = start + row
            if copy_of[i] is not None:
                continue
            later = np.nonzero(sims[row, i + 1 :] >= recommendations.NEAR_COPY)[0] + i + 1
            for j in later:
                if copy_of[j] is None:
                    copy_of[j] = i
    return copy_of


def compute(user_id: int, run_id: int, window_days: int) -> dict[str, int]:
    """Rank the person's pool with their latest taste model, fold copies,
    and replace their stored ranking with it; what was stored. Nothing is
    stored before they have a model."""
    import numpy as np

    rows = db.query(
        recommendations.pool_sql(GATE, KEPT), {**params(user_id), "window_days": window_days}
    )
    if not rows:
        return {"postings": 0, "rows": 0}
    copy_of = _fold(np.array([r["embedding"] for r in rows], dtype=np.float32))
    ids = [r["id"] for r in rows]
    with db.transaction():
        db.execute("DELETE FROM recommended_postings WHERE user_id = %s", (user_id,))
        db.execute(
            """
            INSERT INTO recommended_postings (user_id, job_id, score, rank, kept, copy_of)
            SELECT %s, job_id, score, rank, kept, copy_of
            FROM unnest(%s::bigint[], %s::float8[], %s::int[], %s::bool[], %s::bigint[])
                 AS r(job_id, score, rank, kept, copy_of)
            """,
            (
                user_id,
                ids,
                [float(r["score"]) for r in rows],
                list(range(1, len(rows) + 1)),
                [bool(r["kept"]) for r in rows],
                [ids[c] if c is not None else None for c in copy_of],
            ),
        )
        db.execute(
            "INSERT INTO recommendation_runs (user_id, run_id, model_id, postings) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET "
            "run_id = EXCLUDED.run_id, model_id = EXCLUDED.model_id, "
            "postings = EXCLUDED.postings, computed_at = now()",
            (user_id, run_id, rows[0]["model_id"], len(rows)),
        )
    return {"postings": len(rows), "rows": sum(c is None for c in copy_of)}


# A stored posting still worth showing: open, and not touched since the run.
_STILL = (
    catalog.IS_AVAILABLE.format(job="j")
    + """
    AND j.id NOT IN (
        SELECT job_id FROM user_jobs WHERE user_id = %(user_id)s
        UNION SELECT job_id FROM applications WHERE user_id = %(user_id)s AND job_id IS NOT NULL)
"""
)

_POSTING = """
    j.id, j.url, j.company, j.title, j.locations,
    COALESCE(j.date_posted, j.created_at) AS posted,
    c.comp_min, c.comp_max, c.comp_period, c.comp_currency
"""

# The rows of the person's ranking (a job's copies folded into the row of its
# best-ranked listing), after %(after_rank)s, NULL for the first page.
PAGE = f"""
SELECT {_POSTING}, rp.rank, rp.kept, run.postings, run.run_id
FROM recommendation_runs run
JOIN recommended_postings rp ON rp.user_id = run.user_id
JOIN jobs j ON j.id = rp.job_id
LEFT JOIN job_comp c ON c.url = j.url
WHERE run.user_id = %(user_id)s AND rp.copy_of IS NULL AND {_STILL}
  AND (NOT %(kept_only)s OR rp.kept)
  AND (%(after_rank)s::int IS NULL OR rp.rank > %(after_rank)s)
ORDER BY rp.rank
LIMIT %(limit)s
"""

ROWS = f"""
SELECT count(*) AS n FROM recommended_postings rp JOIN jobs j ON j.id = rp.job_id
WHERE rp.user_id = %(user_id)s AND rp.copy_of IS NULL AND {_STILL}
  AND (NOT %(kept_only)s OR rp.kept)
"""

# The copies folded into each of %(ids)s, best ranked first.
COPIES = f"""
SELECT rp.copy_of, j.id, j.url, j.company, j.title, j.locations
FROM recommended_postings rp JOIN jobs j ON j.id = rp.job_id
WHERE rp.user_id = %(user_id)s AND rp.copy_of = ANY(%(ids)s) AND {_STILL}
ORDER BY rp.rank
"""

# The rows of the person's ranking nearest to the posting %(job_id)s, less
# its near copies and itself, after (%(after_distance)s, %(after_id)s).
SIMILAR = f"""
WITH target AS (
    SELECT pe.embedding FROM jobs t JOIN posting_embeddings pe ON pe.url = t.url
    WHERE t.id = %(job_id)s
),
near AS (
    SELECT {_POSTING}, pe.embedding <=> target.embedding AS distance
    FROM recommended_postings rp
    JOIN jobs j ON j.id = rp.job_id
    JOIN posting_embeddings pe ON pe.url = j.url
    LEFT JOIN job_comp c ON c.url = j.url
    CROSS JOIN target
    WHERE rp.user_id = %(user_id)s AND rp.copy_of IS NULL AND {_STILL}
      AND j.id <> %(job_id)s
      AND 1 - (pe.embedding <=> target.embedding) < %(near_copy)s
),
r AS (SELECT near.*, count(*) OVER () AS total FROM near)
SELECT * FROM r
WHERE %(after_distance)s::float8 IS NULL
   OR r.distance > %(after_distance)s
   OR (r.distance = %(after_distance)s AND r.id > %(after_id)s)
ORDER BY r.distance, r.id
LIMIT %(limit)s
"""
