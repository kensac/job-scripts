"""Embeds every posting with the model the fleet serves itself.

Two kinds of posting are selected, in this order. First, the ones some person
can see now (api.board.visibility.across_users) whose current page has no
vector, or a vector from an older page, or from before its company's text
profile last changed: what the similarity panel reads stays current. Then
every other posting with page text that has never been embedded, newest
first, so the whole corpus is there for analysis. A posting outside the
visible slice is embedded once: a later page or profile does not re-select
it, and its row's content_row_id and profiled_at say what it was embedded
from.

A run takes them BATCH_POSTINGS at a time until nothing is left, embedding
each batch on every server this host names that answers
(core.derivations.local_embeddings), as many at once on each as the server
has slots. So a backfill is one run that drains it, picking up servers that
come and go between batches, and an idle cycle's run is one candidate query.

What is embedded is the posting without the paragraphs its company repeats
across roles, and with the company's name masked
(core.derivations.company_text). Each company's shared paragraphs are a
derived profile, rebuilt here whenever the company has more distinct titles
than its profile counted or was built under an older rule
(company_text.PROFILE_RULE); a row records the profile it was embedded under,
so a rebuilt profile re-selects its company's visible postings. The stored
hash is of the text embedded, so a re-scrape or a rebuilt profile that leaves
that text unchanged is re-stamped, not embedded again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import queue
import threading
from typing import Any, Literal

import requests

from api import db
from api.board import visibility
from core import page_fetches
from core.derivations import company_text
from core.derivations.local_embeddings import (
    LOCAL_EMBEDDING_MODEL,
    EmbeddingUnavailable,
    ServerUnreachable,
    embed_document,
    server_urls,
    slots,
)
from core.store import CONTENT_LATERAL
from tasks.derivations.embeddings import candidate_sql
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

KIND = "embed_postings_local"

CANDIDATES = candidate_sql(visibility.across_users("j.url"), "posting_embeddings")

# Every posting with page text that has no row, newest first. Rows already
# embedded are walked past each run rather than remembered: with all 2.1M
# jobs embedded but one, walking them took 23 s (2026-11-26, probing an
# embeddings table's primary key per job), and the 230k jobs without page
# text cost one page_texts probe each on top.
BACKFILL = f"""
    SELECT j.url, q.content_row_id, NULL::text AS stored_hash, q.input_content, q.content_ref
    FROM jobs j
    {CONTENT_LATERAL.format(url="j.url", columns="id AS content_row_id, input_content, content_ref")}
    WHERE NOT EXISTS (SELECT 1 FROM posting_embeddings e WHERE e.url = j.url)
      AND j.url <> ALL(%(taken)s)
    ORDER BY j.id DESC
    LIMIT %(cap)s
"""

# Postings a batch takes. At the 5.5 KB a page averaged (2026-11-26) that is
# about 11 MB of text held at once. Visible postings lead every batch, so a
# posting that has just become visible waits at most one batch during a
# backfill: about 45 minutes on optiplex's two servers alone (0.75 postings a
# second), half a minute with the rented GPUs (68 a second).
BATCH_POSTINGS = 2000

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
    """Up to `cap` postings: the visible ones that need a vector, then never
    embedded ones to fill the run."""
    rows = db.query(CANDIDATES, {"cap": cap})
    if len(rows) < cap:
        taken = [row["url"] for row in rows]
        rows += db.query(BACKFILL, {"cap": cap - len(rows), "taken": taken})
    rows = page_fetches.resolve(rows)
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
    except ServerUnreachable:
        raise
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


def _servers(urls: list[str]) -> tuple[dict[str, int], list[str]]:
    """The servers that answer, with their slots, and the ones that do not."""
    ready, unreachable = {}, []
    for url in urls:
        try:
            ready[url] = slots(_session(), url)
        except EmbeddingUnavailable:
            logger.warning("embedding server %s is not available this run", url, exc_info=True)
            unreachable.append(url)
    return ready, unreachable


# What a worker thread puts on the results queue as it exits, with the server
# it worked for and whether that server stopped answering.
_Retired = tuple[Literal["retired"], str, bool]


def _embed_all(
    task_id: int,
    servers: dict[str, int],
    jobs: list[dict[str, Any]],
    counts: dict[str, int],
    run_counts: dict[str, Any],
) -> None:
    """Every job through one queue, one thread per server slot, each taking
    the next job as it finishes one, results written as they land. A server
    that stops answering hands its job back and leaves the run, so a laptop
    that goes to sleep costs nothing; the run ends when the queue is empty
    or no server is left."""
    todo: queue.Queue[dict[str, Any]] = queue.Queue()
    for job in jobs:
        todo.put(job)
    results: queue.Queue[dict[str, Any] | _Retired | None] = queue.Queue()
    stop = threading.Event()

    def work(url: str) -> None:
        gone = False
        try:
            while not stop.is_set():
                try:
                    job = todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    results.put(_embed(url, job))
                except ServerUnreachable:
                    todo.put(job)
                    gone = True
                    logger.warning("embedding server %s stopped answering", url, exc_info=True)
                    return
        finally:
            results.put(("retired", url, gone))

    threads = [
        threading.Thread(target=work, args=(url,), daemon=True)
        for url, slot_count in servers.items()
        for _ in range(slot_count)
    ]
    for thread in threads:
        thread.start()
    live, done, pending = len(threads), 0, []
    try:
        while live:
            item = results.get()
            if isinstance(item, tuple):
                live -= 1
                if item[2] and item[1] not in run_counts["unreachable"]:
                    run_counts["unreachable"] = sorted([*run_counts["unreachable"], item[1]])
                continue
            done += 1
            if item is None:
                counts["failed"] += 1
            else:
                pending.append(item)
            if len(pending) >= _WRITE_EVERY:
                counts["written"] += _store(pending)
                pending = []
                written = run_counts["written"] + counts["written"]
                set_progress(
                    task_id,
                    done,
                    len(jobs),
                    f"batch {run_counts['batches'] + 1}: {done} of {len(jobs)}; "
                    f"embedded {written} this run",
                )
            if cancelled(task_id):
                stop.set()
    finally:
        stop.set()
        if pending:
            counts["written"] += _store(pending)


def _batch(
    task_id: int, servers: dict[str, int], cap: int, counts: dict[str, Any]
) -> dict[str, int]:
    """One batch of up to `cap` candidates through `servers`, adding to
    `counts`; returns what this batch did."""
    rows = candidates(cap)
    # Profiles built under an older rule, whether or not their postings are
    # candidates: rebuilding one moves its profiled_at, which makes its
    # postings candidates for the next batch (embeddings._PROFILE_CHANGED).
    older = {
        row["company_key"]
        for row in db.query(
            "SELECT company_key FROM company_text_profiles WHERE rule <> %s",
            (company_text.PROFILE_RULE,),
        )
    }
    keys = {row["company_key"] for row in rows} | older
    batch = {
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
        batch["restamped"] = _restamp(unchanged)
    _embed_all(task_id, servers, jobs, batch, counts)
    for key, value in batch.items():
        counts[key] += value
    return batch


def run(task_id: int, cap: int = BATCH_POSTINGS) -> dict[str, Any]:
    """Batches until one leaves nothing new stored (the backlog is drained,
    or what is left fails), no server answers, or the run is cancelled. The
    servers are asked again before each batch, so one that comes back, or
    goes away, counts from the next batch on. `servers` and `unreachable`
    name every server that answered, and that missed a batch, in the run."""
    urls = server_urls()
    if not urls:
        raise EmbeddingUnavailable("no embedding server configured on this host")
    counts: dict[str, Any] = {
        "batches": 0,
        "selected": 0,
        "written": 0,
        "failed": 0,
        "restamped": 0,
        "profiled": 0,
        "servers": [],
        "unreachable": [],
    }
    while not cancelled(task_id):
        servers, unreachable = _servers(urls)
        counts["servers"] = sorted(set(counts["servers"]) | set(servers))
        counts["unreachable"] = sorted(set(counts["unreachable"]) | set(unreachable))
        if not servers:
            if not counts["batches"]:
                raise EmbeddingUnavailable(f"no embedding server answers: {', '.join(unreachable)}")
            break
        batch = _batch(task_id, servers, cap, counts)
        counts["batches"] += 1
        if not batch["written"] and not batch["restamped"]:
            break
    return counts


async def handle_embed_postings_local(task_id: int, payload: dict[str, Any]) -> None:
    if not db.get_config("local_embedding_enabled"):
        set_progress(task_id, 0, 0, "switched off")
        return
    counts = await asyncio.to_thread(run, task_id)
    embeddable = counts["selected"] - counts["restamped"]
    set_progress(
        task_id,
        counts["written"],
        embeddable,
        f"embedded {counts['written']} of {embeddable}, {counts['failed']} failed, "
        f"{counts['restamped']} unchanged"
        + (f"; not answering: {', '.join(counts['unreachable'])}" if counts["unreachable"] else ""),
        extra=counts,
    )
    logger.info("%s: %s", KIND, counts)
