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

A run takes them `local_embed_batch` at a time until nothing is left, embedding
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
import collections
import hashlib
import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

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
from core.payloads.objects import PayloadUnavailable
from core.store import CONTENT_LATERAL
from tasks.derivations.embeddings import candidate_sql
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

KIND = "embed_postings_local"

CANDIDATES = candidate_sql(visibility.across_users("j.url"), "posting_embeddings")

# Every posting with page text that has no row, newest first, below the
# run's cursor (`below`: walked postings at or above it were stored or
# refused, see _batch), so one run walks the corpus once. A new run walks from the top: with all 2.1M jobs
# embedded but one, that took 23 s (2026-11-26, probing an embeddings table's
# primary key per job), and the 230k jobs without page text cost one
# page_texts probe each on top. Pages the servers refused, or whose text
# cannot be read, are passed over.
BACKFILL = f"""
    SELECT j.id AS job_id, j.url, q.content_row_id, NULL::text AS stored_hash,
           q.input_content, q.content_ref
    FROM jobs j
    {CONTENT_LATERAL.format(url="j.url", columns="id AS content_row_id, input_content, content_ref")}
    WHERE j.id < %(below)s
      AND NOT EXISTS (SELECT 1 FROM posting_embeddings e WHERE e.url = j.url)
      AND NOT EXISTS (
          SELECT 1 FROM posting_embedding_failures pf
          WHERE pf.url = j.url AND pf.content_row_id = q.content_row_id)
      AND j.url <> ALL(%(taken)s)
    ORDER BY j.id DESC
    LIMIT %(cap)s
"""

# A cursor above every job id, so a run's walk starts at the newest job.
_NO_CURSOR = 2**63 - 1

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
    job_embeddings' writer keeps (tasks.derivations.embeddings._store), and
    clears the failures of the postings stored."""
    stored = db.execute_count(
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
    db.execute(
        "DELETE FROM posting_embedding_failures WHERE url = ANY(%s)",
        ([row["url"] for row in rows],),
    )
    return stored


def _refused(jobs: list[dict[str, Any]]) -> None:
    """Records pages the servers refused, so they are not selected again."""
    db.execute(
        """
        INSERT INTO posting_embedding_failures (url, content_row_id)
        SELECT r.url, r.row_id FROM jsonb_to_recordset(%s) AS r(url text, row_id bigint)
        ON CONFLICT (url) DO UPDATE SET
            content_row_id = EXCLUDED.content_row_id, failed_at = now()
        """,
        (db.jsonb([{"url": job["url"], "row_id": job["row_id"]} for job in jobs]),),
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


def _readable(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rows with their page text read, and the rows whose stored text
    cannot be read. One unreadable page costs a read of each row on its own,
    not the batch."""
    cache: dict = {}
    try:
        return page_fetches.resolve(rows, cache=cache), []
    except PayloadUnavailable:
        readable, unreadable = [], []
        for row in rows:
            try:
                readable += page_fetches.resolve([row], cache=cache)
            except PayloadUnavailable:
                logger.warning("page text of %s cannot be read", row["url"], exc_info=True)
                unreadable.append(row)
        return readable, unreadable


def candidates(cap: int, below: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Up to `cap` postings: the visible ones that need a vector, then never
    embedded ones below job id `below` to fill the batch (those carry their
    `job_id`); and apart, the ones whose page text cannot be read."""
    rows = db.query(CANDIDATES, {"cap": cap})
    if len(rows) < cap:
        rows += db.query(
            BACKFILL,
            {"cap": cap - len(rows), "below": below, "taken": [row["url"] for row in rows]},
        )
    rows, unreadable = _readable(rows)
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
    return rows, unreadable


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


def _vector_row(job: dict[str, Any], vector: list[float]) -> dict[str, Any]:
    return {
        "url": job["url"],
        "embedding": str(vector),
        "model": LOCAL_EMBEDDING_MODEL,
        "hash": job["hash"],
        "row_id": job["row_id"],
        "profiled_at": job["profiled_at"],
    }


def _probe(url: str) -> int | None:
    try:
        return slots(_session(), url)
    except EmbeddingUnavailable:
        logger.warning("embedding server %s is not available", url, exc_info=True)
        return None


def _servers(urls: list[str]) -> tuple[dict[str, int], list[str]]:
    """The servers that answer, with their slots, and the ones that do not,
    all asked at once so an absent one costs one connect timeout."""
    with ThreadPoolExecutor(len(urls)) as pool:
        answers = dict(zip(urls, pool.map(_probe, urls), strict=True))
    ready = {url: n for url, n in answers.items() if n is not None}
    return ready, [url for url, n in answers.items() if n is None]


class _Jobs:
    """The jobs of a batch not yet with a server. A server's thread takes the
    first job its server has not already failed on, and waits while there is
    none; a server that has gone takes nothing more."""

    def __init__(self, jobs: list[dict[str, Any]]):
        self._waiting = collections.deque(jobs)
        self._gone: set[str] = set()
        self._stopped = False
        self._changed = threading.Condition()

    def take(self, url: str) -> dict[str, Any] | None:
        with self._changed:
            while True:
                if self._stopped or url in self._gone:
                    return None
                for i, job in enumerate(self._waiting):
                    if url not in job["tried"]:
                        del self._waiting[i]
                        return job
                self._changed.wait()

    def give(self, job: dict[str, Any]) -> None:
        with self._changed:
            self._waiting.append(job)
            self._changed.notify_all()

    def gone(self, url: str, live: set[str]) -> list[dict[str, Any]]:
        """Retires `url`; returns, and drops, the waiting jobs no server in
        `live` is left to try."""
        with self._changed:
            self._gone.add(url)
            stranded = [job for job in self._waiting if live <= job["tried"]]
            self._waiting = collections.deque(j for j in self._waiting if not live <= j["tried"])
            self._changed.notify_all()
            return stranded

    def stop(self) -> None:
        with self._changed:
            self._stopped = True
            self._waiting.clear()
            self._changed.notify_all()


def _embed_all(
    task_id: int,
    servers: dict[str, int],
    jobs: list[dict[str, Any]],
    proven: set[str],
    run_counts: dict[str, Any],
) -> tuple[int, list[dict[str, Any]], set[str]]:
    """Every job through every server, one thread per server slot, stored as
    the vectors land. Returns how many were stored, the jobs to record as
    refused, and the urls of the jobs that were stored or refused.

    Whose fault a failure is decides what happens to the job. A server that
    stops answering hands its job back and leaves the batch. An error from a
    server that has stored something this run (`proven`) is the page's: the
    job is refused. An error from one that has not hands the job back, and
    that server is not given it again, so a misconfigured server never gets a
    good page recorded. A page that two servers stopped answering on is the
    page's fault too (one that crashes or outlasts the server), and is
    refused. A job no server is left to try is set aside for a later batch.
    """
    for job in jobs:
        job["tried"], job["downed"] = set(), set()
    work = _Jobs(jobs)
    results: queue.Queue[tuple[str, str, dict[str, Any] | None, Any]] = queue.Queue()

    def serve(url: str) -> None:
        try:
            while (job := work.take(url)) is not None:
                try:
                    vector = embed_document(_session(), url, job["text"]).vector
                except ServerUnreachable:
                    logger.warning("embedding server %s stopped answering", url, exc_info=True)
                    results.put(("downed", url, job, None))
                    return
                except Exception:
                    logger.warning("embedding %s failed on %s", job["url"], url, exc_info=True)
                    results.put(("error", url, job, None))
                else:
                    results.put(("stored", url, job, vector))
        finally:
            results.put(("retired", url, None, None))

    threads = dict(servers)
    for url, n in servers.items():
        for _ in range(n):
            threading.Thread(target=serve, args=(url,), daemon=True).start()
    live = set(servers)
    # settled: stored, refused, or set aside; finished: stored or refused.
    stored, pending, refused = 0, [], []
    settled: set[str] = set()
    finished: set[str] = set()

    def leave(url: str) -> None:
        if url not in live:
            return
        live.discard(url)
        if url not in run_counts["unreachable"]:
            run_counts["unreachable"] = sorted([*run_counts["unreachable"], url])
        settled.update(job["url"] for job in work.gone(url, live))

    def hand_back(job: dict[str, Any]) -> None:
        if live - job["tried"]:
            work.give(job)
        else:
            settled.add(job["url"])

    try:
        while live and len(settled) < len(jobs):
            kind, url, job, vector = results.get()
            if kind == "retired":
                threads[url] -= 1
                if not threads[url]:
                    leave(url)
                continue
            assert job is not None
            if kind == "stored":
                proven.add(url)
                pending.append(_vector_row(job, vector))
                settled.add(job["url"])
                finished.add(job["url"])
            elif kind == "error":
                job["tried"].add(url)
                if url in proven:
                    refused.append(job)
                    settled.add(job["url"])
                    finished.add(job["url"])
                else:
                    hand_back(job)
            else:
                job["tried"].add(url)
                job["downed"].add(url)
                leave(url)
                if len(job["downed"]) > 1:
                    refused.append(job)
                    settled.add(job["url"])
                    finished.add(job["url"])
                else:
                    hand_back(job)
            if len(pending) >= _WRITE_EVERY:
                stored += _store(pending)
                pending = []
                written = run_counts["written"] + stored
                set_progress(
                    task_id,
                    len(settled),
                    len(jobs),
                    f"batch {run_counts['batches'] + 1}: {len(settled)} of {len(jobs)}; "
                    f"embedded {written} this run",
                )
                if cancelled(task_id):
                    break
    finally:
        work.stop()
        if pending:
            stored += _store(pending)
    return stored, refused, finished


def _batch(
    task_id: int, servers: dict[str, int], cap: int, below: int, proven: set[str], counts: dict
) -> tuple[dict[str, int], int]:
    """One batch of up to `cap` candidates through `servers`, adding to
    `counts`; returns what this batch did and the walk's cursor for the
    next. The cursor moves past the walked postings that were stored or
    refused, and no further than the newest one that was not, so a posting
    set aside is walked again."""
    rows, unreadable = candidates(cap, below)
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
        "selected": len(rows) + len(unreadable),
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
    batch["written"], refused, finished = _embed_all(task_id, servers, jobs, proven, counts)
    refused += [{"url": row["url"], "row_id": row["content_row_id"]} for row in unreadable]
    if refused:
        _refused(refused)
    batch["failed"] = len(refused)
    finished |= {row["url"] for row in unreadable} | {job["url"] for job in unchanged}
    walked = [row for row in [*rows, *unreadable] if "job_id" in row]
    left = [row["job_id"] for row in walked if row["url"] not in finished]
    if left:
        below = max(left) + 1
    elif walked:
        below = min(row["job_id"] for row in walked)
    for key in ("selected", "written", "failed", "restamped", "profiled"):
        counts[key] += batch[key]
    return batch, below


def run(task_id: int, cap: int) -> dict[str, Any]:
    """Batches of up to `cap` until one stores, re-stamps and refuses nothing
    (the backlog is drained, or what is left cannot be placed on a server),
    no server answers, or the run is cancelled. The servers are asked again
    before each batch, so one that comes back, or goes away, counts from the
    next batch on. `servers` and `unreachable` name every server that
    answered, and that missed a batch or stopped answering, in the run."""
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
    # Servers that have stored a vector this run, whose errors are therefore
    # the page's (_embed_all).
    proven: set[str] = set()
    below = _NO_CURSOR
    while not cancelled(task_id):
        servers, unreachable = _servers(urls)
        counts["servers"] = sorted(set(counts["servers"]) | set(servers))
        counts["unreachable"] = sorted(set(counts["unreachable"]) | set(unreachable))
        if not servers:
            if not counts["batches"]:
                raise EmbeddingUnavailable(f"no embedding server answers: {', '.join(unreachable)}")
            break
        batch, below = _batch(task_id, servers, cap, below, proven, counts)
        counts["batches"] += 1
        if not (batch["written"] or batch["restamped"] or batch["failed"]):
            break
    return counts


async def handle_embed_postings_local(task_id: int, payload: dict[str, Any]) -> None:
    if not db.get_config("local_embedding_enabled"):
        set_progress(task_id, 0, 0, "switched off")
        return
    counts = await asyncio.to_thread(run, task_id, int(db.get_config("local_embed_batch")))
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
