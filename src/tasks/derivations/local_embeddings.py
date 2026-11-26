"""Embeds postings with the model the fleet serves itself.

The same postings as the OpenAI sweep (tasks.derivations.embeddings): every
posting some person can see whose current page has no vector, or a vector
from an older page. An unchanged re-scrape is re-stamped (tasks.rescrape).
Each run takes up to `local_embed_per_cycle` of them and embeds them on the
servers this host names (core.derivations.local_embeddings), concurrently
up to the slots each server runs; only a host with servers claims the kind.

What is embedded is the posting without the paragraphs its company repeats
across roles, and with the company's name masked
(core.derivations.company_text). Each company's shared paragraphs are a
derived profile, rebuilt here whenever the company has more distinct titles
than its profile counted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from api import db
from api.board import visibility
from core import page_fetches
from core.derivations import company_text
from core.derivations.embeddings import EMBEDDING_INPUT_CHARS
from core.derivations.local_embeddings import (
    LOCAL_EMBEDDING_MODEL,
    EmbeddingUnavailable,
    embed_document,
    server_urls,
)
from tasks import rescrape
from tasks.derivations.embeddings import candidate_sql
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

KIND = "embed_postings_local"

CANDIDATES = candidate_sql(visibility.across_users("j.url"), "posting_embeddings")

# Requests in flight per server: the slots llama-server is started with
# (`-np`, deploy/embeddings in optiplex-config), so every slot stays busy.
SLOTS_PER_SERVER = 4

# Postings written per statement, so a run that stops part way keeps what it
# embedded and progress moves as it goes.
_WRITE_EVERY = 50

_sessions = threading.local()


def _session() -> requests.Session:
    session = getattr(_sessions, "session", None)
    if session is None:
        session = _sessions.session = requests.Session()
    return session


def _store(rows: list[dict[str, Any]]) -> int:
    """Upserts vectors, never over one computed from a newer page, the guard
    job_embeddings' writer keeps (tasks.derivations.embeddings._store)."""
    return db.execute_count(
        """
        INSERT INTO posting_embeddings (url, embedding, model, content_hash, content_row_id)
        SELECT r.url, r.embedding::halfvec, r.model, r.hash, r.row_id
        FROM jsonb_to_recordset(%s) AS r(
            url text, embedding text, model text, hash text, row_id bigint
        )
        ON CONFLICT (url) DO UPDATE SET
            embedding = EXCLUDED.embedding, model = EXCLUDED.model,
            content_hash = EXCLUDED.content_hash,
            content_row_id = EXCLUDED.content_row_id,
            created_at = now()
        WHERE posting_embeddings.content_row_id IS NULL
           OR posting_embeddings.content_row_id <= EXCLUDED.content_row_id
           OR NOT EXISTS (
               SELECT 1 FROM page_texts p WHERE p.id = posting_embeddings.content_row_id
           )
        """,
        (db.jsonb(rows),),
    )


def candidates(cap: int) -> list[dict[str, Any]]:
    rows = page_fetches.resolve(db.query(CANDIDATES, {"cap": cap}))
    rows = rescrape.drop_unchanged(rows, table="posting_embeddings", limit=EMBEDDING_INPUT_CHARS)
    companies = {
        row["url"]: row["company"]
        for row in db.query(
            "SELECT url, company FROM jobs WHERE url = ANY(%s)", ([r["url"] for r in rows],)
        )
    }
    for row in rows:
        row["company"] = companies.get(row["url"], "")
    return rows


def refresh_profiles(keys: set[str]) -> int:
    """Rebuilds the shared paragraphs of every company in `keys` whose
    distinct titles outnumber its profile's, returning how many were rebuilt.
    A profile reads one posting for each of up to PROFILE_TITLES titles."""
    keys.discard("")
    if not keys:
        return 0
    current = {
        row["k"]: row["titles"]
        for row in db.query(
            "SELECT lower(btrim(company)) AS k, count(DISTINCT lower(btrim(title))) AS titles "
            "FROM jobs WHERE lower(btrim(company)) = ANY(%s) GROUP BY 1",
            (list(keys),),
        )
    }
    known = {
        row["company_key"]: row["titles"]
        for row in db.query(
            "SELECT company_key, titles FROM company_text_profiles WHERE company_key = ANY(%s)",
            (list(keys),),
        )
    }
    stale = [k for k, titles in current.items() if known.get(k, 0) < titles]
    if not stale:
        return 0
    samples = page_fetches.resolve(
        db.query(
            """
            SELECT s.k, s.title, q.input_content, q.content_ref FROM (
                SELECT k, title, url, row_number() OVER (PARTITION BY k ORDER BY id DESC) AS n
                FROM (
                    SELECT DISTINCT ON (lower(btrim(company)), lower(btrim(title)))
                           lower(btrim(company)) AS k, title, url, id
                    FROM jobs WHERE lower(btrim(company)) = ANY(%(stale)s)
                    ORDER BY lower(btrim(company)), lower(btrim(title)), id DESC
                ) newest_per_title
            ) s
            CROSS JOIN LATERAL (
                SELECT q.input_content, q.content_ref FROM page_texts q
                WHERE q.url = s.url AND q.chars > 200 ORDER BY q.id DESC LIMIT 1
            ) q
            WHERE s.n <= %(titles)s
            """,
            {"stale": stale, "titles": company_text.PROFILE_TITLES},
        )
    )
    postings: dict[str, list[tuple[str, str]]] = {k: [] for k in stale}
    for sample in samples:
        postings[sample["k"]].append((sample["title"], sample["input_content"]))
    profiles = {k: company_text.shared_paragraphs(p) for k, p in postings.items()}
    with db.transaction():
        db.execute("DELETE FROM company_shared_paragraphs WHERE company_key = ANY(%s)", (stale,))
        shared = [(k, sha) for k, (_, paragraphs) in profiles.items() for sha in paragraphs]
        if shared:
            db.execute(
                "INSERT INTO company_shared_paragraphs (company_key, paragraph_sha) "
                "SELECT * FROM unnest(%s::text[], %s::bytea[])",
                ([k for k, _ in shared], [sha for _, sha in shared]),
            )
        db.execute(
            "INSERT INTO company_text_profiles (company_key, titles) "
            "SELECT * FROM unnest(%s::text[], %s::int[]) "
            "ON CONFLICT (company_key) DO UPDATE SET titles = EXCLUDED.titles, profiled_at = now()",
            (stale, [current[k] for k in stale]),
        )
    return len(stale)


def shared_paragraphs(keys: set[str]) -> dict[str, set[bytes]]:
    return company_text.shared_by_company(
        db.query(
            "SELECT company_key, paragraph_sha FROM company_shared_paragraphs "
            "WHERE company_key = ANY(%s)",
            (list(keys),),
        )
    )


def _embed(job: tuple[str, dict[str, Any], set[bytes]]) -> dict[str, Any] | None:
    url, row, shared = job
    text = company_text.embedding_text(row["input_content"], row["company"], shared)
    try:
        embedded = embed_document(_session(), url, text)
    except EmbeddingUnavailable:
        logger.warning("embedding failed for %s on %s", row["url"], url, exc_info=True)
        return None
    return {
        "url": row["url"],
        "embedding": str(embedded.vector),
        "model": LOCAL_EMBEDDING_MODEL,
        "hash": row["content_hash"],
        "row_id": row["content_row_id"],
    }


def run(task_id: int, cap: int) -> dict[str, int]:
    urls = server_urls()
    if not urls:
        raise EmbeddingUnavailable("no embedding server configured on this host")
    rows = candidates(cap)
    keys = {company_text.company_key(row["company"]) for row in rows}
    counts = {"selected": len(rows), "written": 0, "failed": 0, "profiled": refresh_profiles(keys)}
    shared = shared_paragraphs(keys)
    pending: list[dict[str, Any]] = []
    jobs = [
        (urls[i % len(urls)], row, shared.get(company_text.company_key(row["company"]), set()))
        for i, row in enumerate(rows)
    ]
    with ThreadPoolExecutor(max_workers=SLOTS_PER_SERVER * len(urls)) as executor:
        for done, embedded in enumerate(executor.map(_embed, jobs), start=1):
            if embedded is None:
                counts["failed"] += 1
            else:
                pending.append(embedded)
            if len(pending) >= _WRITE_EVERY or done == len(jobs):
                counts["written"] += _store(pending) if pending else 0
                pending = []
                set_progress(task_id, done, len(jobs), f"embedded {counts['written']}")
            if cancelled(task_id):
                executor.shutdown(wait=False, cancel_futures=True)
                break
    if pending:
        counts["written"] += _store(pending)
    return counts


async def handle_embed_postings_local(task_id: int, payload: dict[str, Any]) -> None:
    if not db.get_config("local_embedding_enabled"):
        set_progress(task_id, 0, 0, "switched off")
        return
    cap = int(db.get_config("local_embed_per_cycle"))
    counts = await asyncio.to_thread(run, task_id, cap)
    set_progress(
        task_id,
        counts["written"],
        counts["selected"],
        f"embedded {counts['written']} of {counts['selected']}, {counts['failed']} failed",
        extra=counts,
    )
    logger.info("%s: %s", KIND, counts)
