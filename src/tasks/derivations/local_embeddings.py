"""Embeds postings with the model the fleet serves itself.

The same postings as the OpenAI sweep (tasks.derivations.embeddings): every
posting some person can see whose current page has no vector, or a vector
from an older page, or from before its company's text profile last changed.
Each run takes up to `local_embed_per_cycle` of them and embeds them on the
servers this host names (core.derivations.local_embeddings); only a host with
servers claims the kind.

What is embedded is the posting without the paragraphs its company repeats
across roles, and with the company's name masked
(core.derivations.company_text). Each company's shared paragraphs are a
derived profile, rebuilt here whenever the company has more distinct titles
than its profile counted or was built under an older rule
(company_text.PROFILE_RULE); a row records the profile it was embedded under,
so a rebuilt profile re-selects its company's postings. The stored hash is of
the text embedded, so a re-scrape or a rebuilt profile that leaves that text
unchanged is re-stamped, not embedded again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import queue
import threading
from typing import Any

import requests

from api import db
from api.board import visibility
from core import page_fetches
from core.derivations import company_text
from core.derivations.local_embeddings import (
    LOCAL_EMBEDDING_MODEL,
    EmbeddingUnavailable,
    embed_document,
    server_urls,
)
from tasks.derivations.embeddings import candidate_sql
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

KIND = "embed_postings_local"

CANDIDATES = candidate_sql(visibility.across_users("j.url"), "posting_embeddings")

# Requests in flight per server: the slots llama-server is started with
# (`-np`, optiplex-config's embeddings services). Each server has its own
# threads pulling from one queue, so a fast server never waits on a slow one.
SLOTS_PER_SERVER = 4

# Postings written per statement, so a run that stops part way keeps what it
# embedded and progress moves as it goes.
_WRITE_EVERY = 50

# Companies profiled per read: at PROFILE_TITLES texts each and the 5.5 KB a
# page averaged (2026-11-26), about 5 MB of text held at once, with progress
# written between batches.
_PROFILE_BATCH = 25

_sessions = threading.local()


def _session() -> requests.Session:
    session = getattr(_sessions, "session", None)
    if session is None:
        session = _sessions.session = requests.Session()
    return session


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _store(rows: list[dict[str, Any]]) -> int:
    """Upserts vectors, never over one computed from a newer page, the guard
    job_embeddings' writer keeps (tasks.derivations.embeddings._store)."""
    return db.execute_count(
        """
        INSERT INTO posting_embeddings
            (url, embedding, model, content_hash, content_row_id, profiled_at)
        SELECT r.url, r.embedding::halfvec, r.model, r.hash, r.row_id, r.profiled_at
        FROM jsonb_to_recordset(%s) AS r(
            url text, embedding text, model text, hash text, row_id bigint,
            profiled_at timestamptz
        )
        ON CONFLICT (url) DO UPDATE SET
            embedding = EXCLUDED.embedding, model = EXCLUDED.model,
            content_hash = EXCLUDED.content_hash,
            content_row_id = EXCLUDED.content_row_id,
            profiled_at = EXCLUDED.profiled_at,
            created_at = now()
        WHERE posting_embeddings.content_row_id IS NULL
           OR posting_embeddings.content_row_id <= EXCLUDED.content_row_id
           OR NOT EXISTS (
               SELECT 1 FROM page_texts p WHERE p.id = posting_embeddings.content_row_id
           )
        """,
        (db.jsonb(rows),),
    )


def _restamp(rows: list[dict[str, Any]]) -> int:
    """Rows whose embedded text did not change take their new page and
    profile, keeping their vector."""
    return db.execute_count(
        """
        UPDATE posting_embeddings e SET content_row_id = r.row_id, profiled_at = r.profiled_at
        FROM jsonb_to_recordset(%s) AS r(url text, row_id bigint, profiled_at timestamptz)
        WHERE e.url = r.url
        """,
        (db.jsonb(rows),),
    )


def candidates(cap: int) -> list[dict[str, Any]]:
    rows = page_fetches.resolve(db.query(CANDIDATES, {"cap": cap}))
    companies = {
        row["url"]: row
        for row in db.query(
            "SELECT url, company, lower(btrim(company)) AS company_key FROM jobs "
            "WHERE url = ANY(%s)",
            ([r["url"] for r in rows],),
        )
    }
    for row in rows:
        job = companies.get(row["url"], {})
        row["company"] = job.get("company", "")
        row["company_key"] = job.get("company_key", "")
    return rows


def _profile(stale: list[str], current: dict[str, int]) -> None:
    """Rebuilds the profiles of `stale` companies from the newest posting of
    each of up to PROFILE_TITLES titles that has page text."""
    samples = page_fetches.resolve(
        db.query(
            """
            SELECT s.k, s.company, s.title, q.input_content, q.content_ref FROM (
                SELECT k, company, title, url,
                       row_number() OVER (PARTITION BY k ORDER BY id DESC) AS n
                FROM (
                    SELECT DISTINCT ON (lower(btrim(j.company)), lower(btrim(j.title)))
                           lower(btrim(j.company)) AS k, j.company, j.title, j.url, j.id
                    FROM jobs j
                    WHERE lower(btrim(j.company)) = ANY(%(stale)s)
                      AND EXISTS (SELECT 1 FROM page_texts q WHERE q.url = j.url AND q.chars > 200)
                    ORDER BY lower(btrim(j.company)), lower(btrim(j.title)), j.id DESC
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
    names: dict[str, str] = {}
    for sample in samples:
        postings[sample["k"]].append((sample["title"], sample["input_content"]))
        names[sample["k"]] = sample["company"]
    profiles = {k: company_text.shared_paragraphs(names.get(k, k), p) for k, p in postings.items()}
    shared = [(k, sha) for k, (_, paragraphs) in profiles.items() for sha in paragraphs]
    with db.transaction():
        db.execute("DELETE FROM company_shared_paragraphs WHERE company_key = ANY(%s)", (stale,))
        if shared:
            db.execute(
                "INSERT INTO company_shared_paragraphs (company_key, paragraph_sha) "
                "SELECT * FROM unnest(%s::text[], %s::bytea[])",
                ([k for k, _ in shared], [sha for _, sha in shared]),
            )
        db.execute(
            "INSERT INTO company_text_profiles (company_key, titles, rule) "
            "SELECT k, t, %s FROM unnest(%s::text[], %s::int[]) AS s(k, t) "
            "ON CONFLICT (company_key) DO UPDATE SET titles = EXCLUDED.titles, "
            "rule = EXCLUDED.rule, profiled_at = now()",
            (company_text.PROFILE_RULE, stale, [current[k] for k in stale]),
        )


def refresh_profiles(keys: set[str], task_id: int | None = None) -> int:
    """Rebuilds the shared paragraphs of every company in `keys` whose
    distinct titles outnumber its profile's, a batch of companies at a time,
    returning how many were rebuilt."""
    keys = {k for k in keys if k}
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
        row["company_key"]: row
        for row in db.query(
            "SELECT company_key, titles, rule FROM company_text_profiles "
            "WHERE company_key = ANY(%s)",
            (list(keys),),
        )
    }
    stale = sorted(
        k
        for k, titles in current.items()
        if k not in known
        or known[k]["titles"] < titles
        or known[k]["rule"] != company_text.PROFILE_RULE
    )
    for start in range(0, len(stale), _PROFILE_BATCH):
        _profile(stale[start : start + _PROFILE_BATCH], current)
        if task_id is not None:
            done = min(start + _PROFILE_BATCH, len(stale))
            set_progress(task_id, done, len(stale), f"profiled {done} companies")
    return len(stale)


def profiles(keys: set[str]) -> tuple[dict[str, set[bytes]], dict[str, Any]]:
    """Each company's shared paragraphs and the time its profile was built."""
    shared = company_text.shared_by_company(
        db.query(
            "SELECT company_key, paragraph_sha FROM company_shared_paragraphs "
            "WHERE company_key = ANY(%s)",
            (list(keys),),
        )
    )
    built = {
        row["company_key"]: row["profiled_at"]
        for row in db.query(
            "SELECT company_key, profiled_at FROM company_text_profiles "
            "WHERE company_key = ANY(%s)",
            (list(keys),),
        )
    }
    return shared, built


def _embed(url: str, job: dict[str, Any]) -> dict[str, Any] | None:
    try:
        embedded = embed_document(_session(), url, job["text"])
    except Exception:
        # A posting that cannot be embedded, for whatever reason, is left
        # without a row and selected again next run; it never ends the run.
        logger.warning("embedding failed for %s on %s", job["url"], url, exc_info=True)
        return None
    return {
        "url": job["url"],
        "embedding": str(embedded.vector),
        "model": LOCAL_EMBEDDING_MODEL,
        "hash": job["hash"],
        "row_id": job["row_id"],
        "profiled_at": job["profiled_at"],
    }


def _embed_all(task_id: int, urls: list[str], jobs: list[dict[str, Any]], counts: dict) -> None:
    """Every job through one queue, SLOTS_PER_SERVER threads per server each
    taking the next job as it finishes one, results written as they land."""
    todo: queue.Queue[dict[str, Any] | None] = queue.Queue()
    for job in jobs:
        todo.put(job)
    results: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stop = threading.Event()

    def work(url: str) -> None:
        while not stop.is_set():
            try:
                job = todo.get_nowait()
            except queue.Empty:
                return
            results.put(_embed(url, job) if job is not None else None)

    threads = [
        threading.Thread(target=work, args=(url,), daemon=True)
        for url in urls
        for _ in range(SLOTS_PER_SERVER)
    ]
    for thread in threads:
        thread.start()
    pending: list[dict[str, Any]] = []
    try:
        for done in range(1, len(jobs) + 1):
            embedded = results.get()
            if embedded is None:
                counts["failed"] += 1
            else:
                pending.append(embedded)
            if len(pending) >= _WRITE_EVERY:
                counts["written"] += _store(pending)
                pending = []
                set_progress(task_id, done, len(jobs), f"embedded {counts['written']}")
            if cancelled(task_id):
                break
    finally:
        stop.set()
        if pending:
            counts["written"] += _store(pending)


def run(task_id: int, cap: int) -> dict[str, int]:
    urls = server_urls()
    if not urls:
        raise EmbeddingUnavailable("no embedding server configured on this host")
    rows = candidates(cap)
    # Profiles built under an older rule, whether or not their postings are
    # candidates: rebuilding one moves its profiled_at, which makes its
    # postings candidates for the next run (embeddings._PROFILE_CHANGED).
    older = {
        row["company_key"]
        for row in db.query(
            "SELECT company_key FROM company_text_profiles WHERE rule <> %s",
            (company_text.PROFILE_RULE,),
        )
    }
    keys = {row["company_key"] for row in rows} | older
    counts = {
        "selected": len(rows),
        "written": 0,
        "failed": 0,
        "restamped": 0,
        "profiled": refresh_profiles(keys, task_id),
    }
    shared, built = profiles(keys)
    jobs, unchanged = [], []
    for row in rows:
        text = company_text.embedding_text(
            row["input_content"], row["company"], shared.get(row["company_key"], set())
        )
        job = {
            "url": row["url"],
            "text": text,
            "hash": _hash(text),
            "row_id": row["content_row_id"],
            "profiled_at": built.get(row["company_key"]),
        }
        (unchanged if row["stored_hash"] == job["hash"] else jobs).append(job)
    if unchanged:
        counts["restamped"] = _restamp(unchanged)
    _embed_all(task_id, urls, jobs, counts)
    return counts


async def handle_embed_postings_local(task_id: int, payload: dict[str, Any]) -> None:
    if not db.get_config("local_embedding_enabled"):
        set_progress(task_id, 0, 0, "switched off")
        return
    cap = int(db.get_config("local_embed_per_cycle"))
    counts = await asyncio.to_thread(run, task_id, cap)
    embeddable = counts["selected"] - counts["restamped"]
    set_progress(
        task_id,
        counts["written"],
        embeddable,
        f"embedded {counts['written']} of {embeddable}, {counts['failed']} failed, "
        f"{counts['restamped']} unchanged",
        extra=counts,
    )
    logger.info("%s: %s", KIND, counts)
