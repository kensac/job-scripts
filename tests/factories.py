"""Builders for the rows a test needs, so a test says what it is about.

Every test in this suite used to open with fifteen lines of INSERT before the
first assertion, which made the setup the loudest part of the file and meant a
schema change touched every test. These return ids and take only the fields the
caller actually cares about; everything else gets a sane default.

Nothing here mocks anything - they write to the real database the suite runs
against, because the behaviour under test is mostly SQL.
"""

from __future__ import annotations

import io
import itertools
from typing import Any

from api import db

_seq = itertools.count(1)


def _next(prefix: str) -> str:
    return f"{prefix}{next(_seq)}"


def make_user(
    *,
    sub: str | None = None,
    email: str | None = None,
    groups: list[str] | None = None,
) -> int:
    sub = sub or _next("sub-")
    email = email or f"{sub}@example.test"
    row = db.query_one(
        """
        INSERT INTO users (sub, email, name, groups) VALUES (%s, %s, %s, %s)
        ON CONFLICT (sub) DO UPDATE SET email = EXCLUDED.email
        RETURNING id
        """,
        (sub, email, "", groups or []),
    )
    assert row is not None
    return row["id"]


def make_source(name: str | None = None, *, active: bool = True) -> str:
    name = name or _next("source-")
    db.execute(
        "INSERT INTO sources (name, listings_url, active) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active",
        (name, f"https://{name}.test/jobs.json", active),
    )
    return name


def make_job(
    *,
    url: str | None = None,
    source: str = "src-test",
    company: str = "Acme",
    title: str = "Engineer",
    active: bool = True,
    uploaded_by: int | None = None,
    comp_min: int | None = None,
    comp_max: int | None = None,
) -> int:
    url = url or f"https://jobs.test/{_next('j')}"
    row = db.query_one(
        """
        INSERT INTO jobs (url, raw_url, source, company, title, active, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (url) DO UPDATE SET active = EXCLUDED.active
        RETURNING id
        """,
        (url, url, source, company, title, active, uploaded_by),
    )
    assert row is not None
    if comp_min is not None or comp_max is not None:
        make_comp(url, comp_min=comp_min, comp_max=comp_max)
    return row["id"]


def make_comp(url: str, **fields: Any) -> None:
    """The pay the comp derivation read off a posting (job_comp)."""
    columns = ["url", *fields]
    db.execute(
        f"INSERT INTO job_comp ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
        (url, *fields.values()),
    )


def make_verdict(
    url: str,
    check_type: str,
    status: str = "passed",
    *,
    prompt_hash: str | None = None,
    content: str | None = None,
    reason: str = "",
) -> None:
    """Append a verdict. Latest row per (url, check_type) wins, so calling this
    twice is how a test expresses "the verdict changed"."""
    from core.store import add_ai_result

    add_ai_result(
        url,
        status,
        reason,
        check_type,
        prompt_hash=prompt_hash,
        input_content=content,
        model="gpt-5-nano",
    )


def answer_pointers(url: str) -> list[dict[str, Any]]:
    """Every answer for `url` in id order, with what it points at: `rebuilt`
    is its input as core.answer_inputs rebuilds it from its fetch, beside
    `copy`, the input_content it stored; `call_tokens` is its call's."""
    from core import answer_inputs

    return db.query(
        f"""
        SELECT q.check_type, q.page_fetch_id, q.model_call_id,
               {answer_inputs.sql("q", "f.content")} AS rebuilt, q.input_content AS copy,
               m.total_tokens AS call_tokens, m.duration_ms AS call_duration_ms
        FROM ai_queries q
        LEFT JOIN page_fetches f ON f.id = q.page_fetch_id
        LEFT JOIN model_calls m ON m.id = q.model_call_id
        WHERE q.url = %s ORDER BY q.id
        """,
        (url,),
    )


def make_fetch(
    url: str, *, content: str | None = None, status: str = "passed", method: str = "scraped"
) -> int:
    """Append a page fetch, the way the fetchers do. The newest fetch with
    text is the page every reader sees."""
    from core import page_fetches

    return page_fetches.record(url, status, method, content)


def make_ready_job(
    *,
    source: str = "src-test",
    content: str = "a long job description " * 20,
    closed: str = "passed",
    clearance: str = "passed",
    **job_kwargs: Any,
) -> tuple[int, str]:
    """A job that has everything the sweeps require: cached content plus a
    decided closed and clearance verdict. This is the state most board and
    filter tests actually want, and building it by hand is where they go wrong.
    """
    job_id = make_job(source=source, **job_kwargs)
    row = db.query_one("SELECT url FROM jobs WHERE id = %s", (job_id,))
    assert row is not None
    url = row["url"]
    make_fetch(url, content=content)
    if closed:
        make_verdict(url, "closed", closed)
    if clearance:
        make_verdict(url, "clearance", clearance)
    return job_id, url


def make_filter(
    user_id: int,
    *,
    name: str | None = None,
    prompt: str = "must be a backend role",
    enabled: bool = True,
    on_ambiguous: str = "pass",
) -> dict[str, Any]:
    """Returns the filter row, because tests almost always need prompt_hash -
    it is what verdicts are keyed on."""
    from core.filters import build_custom_instructions, compute_prompt_hash

    name = name or _next("filter-")
    prompt_hash = compute_prompt_hash(build_custom_instructions(prompt, on_ambiguous))
    row = db.query_one(
        """
        INSERT INTO user_filters (user_id, name, prompt, on_ambiguous, fail_closed,
                                  enabled, prompt_hash)
        VALUES (%s, %s, %s, %s, FALSE, %s, %s)
        RETURNING id, name, prompt, on_ambiguous, enabled, prompt_hash
        """,
        (user_id, name, prompt, on_ambiguous, enabled, prompt_hash),
    )
    assert row is not None
    return row


def subscribe(user_id: int, source: str) -> None:
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, source),
    )


def make_board_row(user_id: int, job_id: int, *, status: str | None = "Saved") -> None:
    """A row the person acted on, which is theirs whatever the criteria or
    the verdicts say. The default is a status because a bare row grants
    nothing: the worker materialises one for every passing posting, and
    #424 made those obey the verdicts like any other posting after 614
    rejected postings stayed on a board through them. Pass status=None
    for that untouched kind when a test is about it."""
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, job_id) DO UPDATE SET status = EXCLUDED.status",
        (user_id, job_id, status),
    )


def filter_config(flt: dict[str, Any]) -> int:
    """The config_id a chunk of this filter carries (api.run_configs)."""
    from api import run_configs

    return run_configs.intern(
        run_configs.FILTER, {k: flt[k] for k in ("name", "prompt", "on_ambiguous", "prompt_hash")}
    )


def board_config(payload: dict[str, Any]) -> dict[str, Any]:
    """A board run payload with its board settings moved behind config_id."""
    from api import run_configs

    keys = ("prompt", "prompt_hash", "on_ambiguous", "fail_closed", "sources", "criteria")
    settings = {k: payload[k] for k in keys if k in payload}
    rest = {k: v for k, v in payload.items() if k not in settings}
    return {**rest, "config_id": run_configs.intern(run_configs.BOARD, settings)}


def make_task(kind: str, payload: dict[str, Any] | None = None, *, status: str = "pending") -> int:
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES (%s, %s, %s) RETURNING id",
        (kind, db.jsonb(payload or {}), status),
    )
    assert row is not None
    return row["id"]


def make_requirements(
    url: str,
    *,
    has_requirements: bool = True,
    skills_required: list[str] | None = None,
    skills_preferred: list[str] | None = None,
    **fields: Any,
) -> None:
    """A job_requirements row plus its job_skills rows, written the way the
    handler writes them - canonical skill beside the raw text - so a test that
    passes here is testing the same shape production reads."""
    from core import skills as skills_lib

    columns = {
        "yoe_min": None,
        "yoe_max": None,
        "degree_min": None,
        "degree_required": False,
        "degree_fields": [],
        "enrollment_required": False,
        "seniority": None,
        "employment_type": None,
        "clearance": None,
        "citizenship_required": False,
        "sponsorship": None,
        **fields,
    }
    names = ", ".join(columns)
    placeholders = ", ".join(f"%({k})s" for k in columns)
    # Stamped with the row the content came from, as the handler does. Without
    # it the row reads as "extracted from we-do-not-know-which page", which the
    # sweep correctly treats as needing a re-read.
    current = db.query_one(
        "SELECT id FROM page_texts WHERE url = %s AND length(input_content) > 200 "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    db.execute(
        f"INSERT INTO job_requirements (url, has_requirements, content_row_id, {names}) "
        f"VALUES (%(url)s, %(has)s, %(row_id)s, {placeholders})",
        {
            "url": url,
            "has": has_requirements,
            "row_id": (current or {}).get("id"),
            **columns,
        },
    )
    for kind, raws in (("required", skills_required), ("preferred", skills_preferred)):
        for raw in raws or []:
            skill = skills_lib.canonical(raw)
            if skill:
                db.execute(
                    "INSERT INTO job_skills (url, kind, skill, skill_raw) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (url, kind, skill, raw),
                )


def make_embedding(url: str, vector: list[float] | None = None, *, seed: float = 0.0) -> None:
    """A job_embeddings row. `seed` shifts the vector along one axis, which is
    enough to order neighbours deterministically without a test having to write
    1536 numbers out."""
    from core.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL

    if vector is None:
        vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
        vector[1] = seed
    assert len(vector) == EMBEDDING_DIMENSIONS
    current = db.query_one(
        "SELECT id FROM page_texts WHERE url = %s AND length(input_content) > 200 "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    db.execute(
        "INSERT INTO job_embeddings (url, embedding, model, content_hash, content_row_id) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET embedding = EXCLUDED.embedding",
        (url, str(vector), EMBEDDING_MODEL, "hash", (current or {}).get("id")),
    )


def finished(collector):
    """Adapts a fake that returns results alone into the shape collect_finished_batches
    returns: everything it yields counts as finished, nothing stays parked."""

    async def _collect(batch_ids, on_event=None):
        return await collector(batch_ids, on_event), []

    return _collect


def make_batch_result(
    task_id: int,
    spec,
    *,
    text: str | None = None,
    error: str | None = None,
    usage: dict | None = None,
    model: str | None = None,
    batch_id: str | None = None,
):
    from api.ai import batch_results
    from core.batch import BatchResult

    batch_id = batch_id or f"batch-{task_id}"
    # The batch row its submission would have written, with the payer the
    # submitting task names, so the checkpoint books the paid result to the
    # ledger as it does in production.
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model, payer, payer_id) "
        "SELECT %(bid)s, t.id, CASE WHEN t.kind LIKE 'application%%' THEN 'application' "
        "     WHEN t.kind LIKE 'run_managed_board%%' THEN 'managed_board' "
        "     WHEN t.kind LIKE 'run_filter%%' THEN 'filter' ELSE t.kind END, %(model)s, "
        "CASE WHEN t.payload ? 'user_id' THEN 'user' "
        "     WHEN t.payload ? 'managed_board_id' THEN 'managed_board' ELSE 'fleet' END, "
        "COALESCE((t.payload->>'user_id')::bigint, (t.payload->>'managed_board_id')::bigint) "
        "FROM tasks t WHERE t.id = %(task)s ON CONFLICT (provider_batch_id) DO NOTHING",
        {"bid": batch_id, "model": model, "task": task_id},
    )
    batch_results.snapshot_specs(task_id, [spec])
    batch_results.checkpoint(
        task_id,
        [
            BatchResult(
                spec.custom_id, text=text, error=error, usage=usage, model=model, batch_id=batch_id
            )
        ],
        [],
    )
    return next(
        result
        for result in batch_results.unconsumed(task_id)
        if result.batch_id == batch_id and result.custom_id == spec.custom_id
    )


class ObjectClient:
    def __init__(self):
        self.objects = {}
        self.fail_put = False
        self.fail_get = False
        self.after_put = lambda: None

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        if self.fail_put:
            raise OSError("upload failed")
        self.objects[Bucket, Key] = Body
        self.after_put()

    def get_object(self, *, Bucket, Key):
        if self.fail_get:
            raise OSError("read failed")
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}
