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

A run takes them `local_embed_batch` at a time, resuming the walk of the
corpus where the last run left it (_walk_start), and embeds each batch on
every server this host names that answers (core.derivations.local_embeddings),
as many at once on each as the server has slots. So a backfill is one run,
or a few if deploys interrupt it, picking up servers that come and go
between batches.

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
import dataclasses
import hashlib
import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
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
    read_frame,
    server_urls,
    slots,
)
from core.payloads.objects import PayloadUnavailable, shared_store
from core.store import CONTENT_LATERAL
from tasks.derivations.embeddings import candidate_sql
from tasks.runtime import cancelled, checkpoint, set_progress

logger = logging.getLogger(__name__)

KIND = "embed_postings_local"

CANDIDATES = candidate_sql(visibility.across_users("j.url"), "posting_embeddings")

# Every posting with page text that has no row, newest first, below the
# run's cursor (`below`, see _prepare and run), so one run walks the corpus once. A new
# run walks from the top, so an idle run costs this walk: with all 2.1M jobs
# embedded but one it took 23 s (2026-11-26, probing an embeddings table's
# primary key per job), and the 230k jobs without page text cost one
# page_texts probe each on top.
BACKFILL = f"""
    SELECT j.id AS job_id, j.url, q.content_row_id, NULL::text AS stored_hash,
           q.input_content, q.content_ref
    FROM jobs j
    {CONTENT_LATERAL.format(url="j.url", columns="id AS content_row_id, input_content, content_ref")}
    WHERE j.id < %(below)s
      AND NOT EXISTS (SELECT 1 FROM posting_embeddings e WHERE e.url = j.url)
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
# page averaged (2026-11-26), about 5 MB of text held at once; progress (for
# the batch a run waits on) and a stop are checked between reads.
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


def _page_fault(exc: PayloadUnavailable) -> bool:
    """Whether object storage answered and the page is what is wrong, as
    against storage not answering. The page's: a failed integrity check or a
    missing member (no cause), a malformed reference or body (ValueError,
    TypeError), or the store refusing that one object (an HTTP 4xx other than
    a timeout or throttling). Storage's: a connection error, a timeout, a
    5xx, throttling. Configuration is checked before a run reads any page
    (run), so a missing setting is never taken for a bad page."""
    cause = exc.__cause__
    if cause is None or isinstance(cause, (ValueError, TypeError)):
        return True
    # botocore's ClientError, read by its shape: importing botocore here would
    # load the SDK into every worker at start (tests/test_lazy_imports.py).
    response = getattr(cause, "response", None)
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return (
            isinstance(status, int)
            and HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
            and status not in (HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS)
        )
    return False


def _readable(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rows with their page text read, and the rows whose page cannot be
    read. One bad page costs a read of each row on its own, not the batch;
    storage that does not answer raises, failing the run, so an outage is
    neither waited out row by row nor taken for bad pages."""
    cache: dict = {}
    try:
        return page_fetches.resolve(rows, cache=cache), []
    except PayloadUnavailable as exc:
        if not _page_fault(exc):
            raise
        # A store that cannot be built (a missing setting) is not a bad page.
        shared_store()
    readable, unread = [], []
    for row in rows:
        try:
            readable += page_fetches.resolve([row], cache=cache)
        except PayloadUnavailable as exc:
            if not _page_fault(exc):
                raise
            logger.warning("page text of %s cannot be read", row.get("url"), exc_info=True)
            unread.append(row)
    return readable, unread


def candidates(
    cap: int, below: int, skip: dict[str, int | None] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Up to `cap` postings: the visible ones that need a vector, then never
    embedded ones below job id `below` to fill the batch (those carry their
    `job_id`), leaving out the postings in `skip` (url to job id, None for a
    visible one); apart, the ones whose page cannot be read; and whether the
    walk is exhausted (asked for postings and found fewer)."""
    skip = skip or {}
    visible_skipped = sum(1 for job_id in skip.values() if job_id is None)
    visible = [
        row
        for row in db.query(CANDIDATES, {"cap": cap + visible_skipped})
        if row["url"] not in skip
    ][:cap]
    walked: list[dict[str, Any]] = []
    exhausted = False
    if len(visible) < cap:
        wanted = cap - len(visible)
        walked = db.query(
            BACKFILL,
            {
                "cap": wanted,
                "below": below,
                # A visible failure can be walked to; a walked one only from
                # below the cursor, where a set-aside posting newer than it
                # moved the cursor back.
                "taken": [row["url"] for row in visible]
                + [url for url, job_id in skip.items() if job_id is None or job_id < below],
            },
        )
        exhausted = len(walked) < wanted
    rows, unread = _readable(visible + walked)
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
    return rows, unread, exhausted


def _profile(stale: list[str], current: dict[str, int]) -> None:
    """Rebuilds the profiles of `stale` companies from the newest posting of
    each of up to PROFILE_TITLES titles that has page text, leaving out a
    sample whose page cannot be read (_readable) and the title with it."""
    samples, unread = _readable(
        db.query(
            """
            SELECT s.k, s.company, s.title, s.url, q.input_content, q.content_ref FROM (
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
    # A title whose sample could not be read is not counted, so the profile
    # reads as short of the company's titles and is rebuilt next time.
    lost = collections.Counter(sample["k"] for sample in unread)
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
            (company_text.PROFILE_RULE, stale, [current[k] - lost[k] for k in stale]),
        )


def refresh_profiles(
    keys: set[str], task_id: int | None = None, stop: threading.Event | None = None
) -> int:
    """Rebuilds the shared paragraphs of every company in `keys` whose
    distinct titles outnumber its profile's, a batch of companies at a time,
    returning how many were rebuilt; with `task_id`, reporting progress
    between batches, and with `stop`, stopping between batches once set."""
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
        if stop is not None and stop.is_set():
            return start
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
    """The server's slots, having read its frame afresh (read_frame), or None
    when it does not answer or cannot be framed."""
    try:
        count = slots(_session(), url)
        read_frame(_session(), url)
        return count
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
    first job its server has not already tried, and waits while there is
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
    task_id: int, servers: dict[str, int], jobs: list[dict[str, Any]], run_counts: dict[str, Any]
) -> tuple[int, set[str], set[str], list[dict[str, Any]]]:
    """Every job through every server, one thread per server slot, stored as
    the vectors land. Returns how many rows were written, the urls whose
    vectors were computed, the urls of the jobs that failed (errored on every
    server still answering, or were in flight on two servers that went down:
    a page that crashes or outlasts a server), and the jobs set aside (left
    when no server that had not tried them was). A failed job is skipped for
    the rest of the run and tried again on the walk's next pass, so no guess
    about whose fault it was outlasts the pass; a set-aside one goes into the
    next batch.

    A server that goes down hands back every job it held and takes no more;
    one that errors on a job hands it to a server that has not tried it. The
    batch ends when every job is stored or failed, or when every thread has
    stopped, so a vector computed before its server went down is still
    stored."""
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
            results.put(("stopped", url, None, None))

    threads = sum(servers.values())
    for url, n in servers.items():
        for _ in range(n):
            threading.Thread(target=serve, args=(url,), daemon=True).start()
    live = set(servers)
    stored, pending, seen = 0, [], 0
    embedded: set[str] = set()
    settled: set[str] = set()
    failed: set[str] = set()

    def leave(url: str) -> None:
        if url not in live:
            return
        live.discard(url)
        if url not in run_counts["unreachable"]:
            run_counts["unreachable"] = sorted([*run_counts["unreachable"], url])
        for job in work.gone(url, live):
            settle_untried(job)

    def settle_untried(job: dict[str, Any]) -> None:
        """A job no server still answering has left to try: failed if one
        errored on it, otherwise only left for a later batch."""
        if job["tried"] - job["downed"]:
            failed.add(job["url"])
        settled.add(job["url"])

    def hand_back(job: dict[str, Any]) -> None:
        if live - job["tried"]:
            work.give(job)
        else:
            settle_untried(job)

    try:
        while threads and len(settled) < len(jobs):
            kind, url, job, vector = results.get()
            if kind == "stopped":
                threads -= 1
                continue
            assert job is not None
            seen += 1
            job["tried"].add(url)
            if kind == "stored":
                pending.append(_vector_row(job, vector))
                embedded.add(job["url"])
                settled.add(job["url"])
            elif kind == "error":
                hand_back(job)
            else:
                job["downed"].add(url)
                leave(url)
                if len(job["downed"]) > 1:
                    failed.add(job["url"])
                    settled.add(job["url"])
                else:
                    hand_back(job)
            if len(pending) >= _WRITE_EVERY:
                stored += _store(pending)
                pending = []
                set_progress(
                    task_id,
                    len(settled),
                    len(jobs),
                    f"batch {run_counts['batches'] + 1}: {len(settled)} of {len(jobs)}; "
                    f"embedded {run_counts['written'] + stored} this run",
                )
            if seen % _WRITE_EVERY == 0 and cancelled(task_id):
                break
    finally:
        work.stop()
        if pending:
            stored += _store(pending)
    aside = [job for job in jobs if job["url"] not in embedded and job["url"] not in failed]
    return stored, embedded, failed, aside


@dataclasses.dataclass(frozen=True)
class _Prepared:
    """A batch ready for the servers: its jobs with their stripped text, the
    re-scrapes whose text did not change, the postings whose page could not
    be read, where its walk reached and whether that exhausted it, the
    visible postings in it (read or not), and the profiles rebuilt for it."""

    jobs: list[dict[str, Any]]
    unchanged: list[dict[str, Any]]
    unread: list[dict[str, Any]]
    walked_to: int
    exhausted: bool
    visible: frozenset[str]
    profiled: int


def _prepare(
    cap: int,
    below: int,
    skip: dict[str, int | None],
    in_flight: frozenset[str],
    stop: threading.Event,
    task_id: int | None = None,
) -> _Prepared | None:
    """Selects up to `cap` candidates below `below`, leaving out `skip` (the
    run's failures) and `in_flight` (the visible postings of the batch the
    servers are embedding, which still need a vector and would otherwise be
    selected twice), rebuilds the profiles they need, and strips their text.
    Reads and profile writes only, so it can run while another batch is on
    the servers. Returns None once `stop` is set, checked between steps, so a
    run that ends does not wait for a batch it will not embed; with
    `task_id`, reports profile progress (the first batch, which the run
    waits for)."""
    if stop.is_set():
        return None
    rows, unread, exhausted = candidates(cap, below, {**skip, **dict.fromkeys(in_flight)})
    # Profiles built under an older rule, whether or not their postings are
    # candidates: rebuilding one moves its profiled_at, which makes its
    # postings candidates for a later batch (embeddings._PROFILE_CHANGED).
    older = {
        row["company_key"]
        for row in db.query(
            "SELECT company_key FROM company_text_profiles WHERE rule <> %s",
            (company_text.PROFILE_RULE,),
        )
    }
    keys = {row["company_key"] for row in rows} | older
    if stop.is_set():
        return None
    profiled = refresh_profiles(keys, task_id, stop)
    if stop.is_set():
        return None
    shared, built = profiles(keys)
    jobs, unchanged = [], []
    for row in rows:
        text = company_text.embedding_text(
            row["input_content"], row["company"], shared.get(row["company_key"], set())
        )
        job = {
            "url": row["url"],
            "job_id": row.get("job_id"),
            "text": text,
            "hash": _hash(text),
            "row_id": row["content_row_id"],
            "profiled_at": built.get(row["company_key"]),
        }
        (unchanged if row["stored_hash"] == job["hash"] else jobs).append(job)
    walked = [row["job_id"] for row in [*rows, *unread] if "job_id" in row]
    return _Prepared(
        jobs=jobs,
        unchanged=unchanged,
        unread=unread,
        walked_to=min(walked) if walked else below,
        exhausted=exhausted,
        visible=frozenset(row["url"] for row in [*rows, *unread] if "job_id" not in row),
        profiled=profiled,
    )


def _embed_prepared(
    task_id: int,
    servers: dict[str, int],
    prepared: _Prepared,
    carried: list[dict[str, Any]],
    skip: dict[str, int | None],
    counts: dict[str, Any],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Embeds a prepared batch, with the jobs the last batch set aside, adding
    to `counts` and to `skip` (the run's failures, with their job ids);
    returns what the batch did and the jobs it set aside."""
    batch = {"written": 0, "failed": 0, "restamped": 0, "profiled": prepared.profiled}
    if prepared.unchanged:
        batch["restamped"] = _restamp(prepared.unchanged)
    jobs = [*carried, *prepared.jobs]
    batch["written"], _, failed, aside = _embed_all(task_id, servers, jobs, counts)
    failed_jobs = [job for job in jobs if job["url"] in failed] + [
        {"url": row["url"], "job_id": row.get("job_id")} for row in prepared.unread
    ]
    batch["failed"] = len(failed_jobs)
    skip.update({job["url"]: job["job_id"] for job in failed_jobs})
    for key in ("written", "failed", "restamped", "profiled"):
        counts[key] += batch[key]
    return batch, aside


def _walk_start() -> int:
    """Where the walk resumes: the cursor the latest run left in its payload
    (a requeued run's own), or the top if none did or that run's walk was
    exhausted. So failures are retried once a pass, not at the head of every
    run, and a run killed by a deploy loses nothing."""
    row = db.query_one(
        "SELECT payload->'below' AS below FROM tasks "
        "WHERE kind = %s AND payload ? 'below' ORDER BY id DESC LIMIT 1",
        (KIND,),
    )
    below = row and row["below"]
    return below if isinstance(below, int) else _NO_CURSOR


def run(task_id: int) -> dict[str, Any]:
    """Batches of `local_embed_batch` until the walk of the corpus is
    exhausted, a batch stores and re-stamps nothing (every server is
    failing, or what is left cannot be placed on one), no server answers,
    the sweep is switched off, or the run is cancelled.

    The next batch is prepared (selected, its companies profiled, its text
    stripped) while the servers embed the current one, so they wait only for
    the first. Settings are read before each batch is prepared, since a
    backfill run lasts days, so a new batch size applies from the batch
    after the one already prepared. The walk's cursor is checkpointed after each batch, never
    past a posting set aside, and a batch that stores nothing leaves it where
    it was. The servers are asked again before each batch, so one that comes
    back, or goes away, counts from the next batch on. `servers` and
    `unreachable` name every server that answered, and that missed a batch
    or stopped answering, in the run."""
    urls = server_urls()
    if not urls:
        raise EmbeddingUnavailable("no embedding server configured on this host")
    counts: dict[str, Any] = {
        "batches": 0,
        "written": 0,
        "failed": 0,
        "restamped": 0,
        "profiled": 0,
        "servers": [],
        "unreachable": [],
    }
    below = _walk_start()
    skip: dict[str, int | None] = {}
    carried: list[dict[str, Any]] = []
    current: _Prepared | None = None
    stop = threading.Event()
    with ThreadPoolExecutor(1) as preparing:
        try:
            while not cancelled(task_id) and db.get_config("local_embedding_enabled"):
                servers, unreachable = _servers(urls)
                counts["servers"] = sorted(set(counts["servers"]) | set(servers))
                counts["unreachable"] = sorted(set(counts["unreachable"]) | set(unreachable))
                if not servers:
                    if not counts["batches"]:
                        raise EmbeddingUnavailable(
                            f"no embedding server answers: {', '.join(unreachable)}"
                        )
                    break
                cap = int(db.get_config("local_embed_batch"))
                if current is None:
                    current = _prepare(cap, below, skip, frozenset(), stop, task_id)
                    assert current is not None
                upcoming = None
                if not current.exhausted:
                    in_flight = current.visible | {
                        job["url"] for job in carried if job["job_id"] is None
                    }
                    upcoming = preparing.submit(
                        _prepare, cap, current.walked_to, dict(skip), frozenset(in_flight), stop
                    )
                batch, carried = _embed_prepared(task_id, servers, current, carried, skip, counts)
                counts["batches"] += 1
                progressed = bool(batch["written"] or batch["restamped"])
                if progressed:
                    # Not past a set-aside posting, should the run end before
                    # the next batch takes it.
                    aside = [job["job_id"] for job in carried if job["job_id"] is not None]
                    below = max(aside) + 1 if aside else current.walked_to
                # An exhausted walk starts again from the top, unless postings
                # are left over, or every posting the batch had failed (the
                # servers, more likely than the pages): those are retried first.
                restart = current.exhausted and not carried and (progressed or not current.jobs)
                ours = checkpoint(
                    task_id,
                    counts["written"],
                    counts["written"] + counts["failed"],
                    f"batch {counts['batches']}: embedded {counts['written']} this run",
                    counts,
                    payload={"below": None if restart else below},
                )
                if not ours or upcoming is None or not progressed:
                    break
                current = upcoming.result()
                if current is None:
                    break
        finally:
            stop.set()
    return counts


async def handle_embed_postings_local(task_id: int, payload: dict[str, Any]) -> None:
    if not db.get_config("local_embedding_enabled"):
        set_progress(task_id, 0, 0, "switched off")
        return
    counts = await asyncio.to_thread(run, task_id)
    set_progress(
        task_id,
        counts["written"],
        counts["written"] + counts["failed"],
        f"embedded {counts['written']}, {counts['failed']} failed, "
        f"{counts['restamped']} unchanged"
        + (f"; not answering: {', '.join(counts['unreachable'])}" if counts["unreachable"] else ""),
        extra=counts,
    )
    logger.info("%s: %s", KIND, counts)
