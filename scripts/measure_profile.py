"""Measure the shape of production, so generated test data is derived from it.

A fixture cannot falsify the assumption it was built from. The way out is not
to stop generating rows, it is to stop generating them from someone's
expectation: this reads production and writes down what is actually there -
which values a column holds, how often it is null, how long its strings are,
how far back its timestamps go - and `tests/corpus.py` builds a corpus that
reproduces those shapes.

Two commands, one file, because they must never disagree about the format:

    python scripts/measure_profile.py            # rewrite tests/production_profile.json
    python scripts/measure_profile.py --check    # fail if production has drifted past it

`--check` is the half that keeps the property. A profile written once is an
assumption again by the following month; re-measuring on a schedule and
failing on a shape the generator cannot produce is what makes drift loud.

NOTHING IDENTIFYING IS WRITTEN. A column only has its literal values recorded
when it is low-cardinality, short, and passes `_identifying` - everything else
is reduced to lengths and character classes. The profile is committed to the
repository, so that rule is the whole safety argument.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import dotenv
import psycopg
from psycopg.rows import dict_row

PROFILE_PATH = Path(__file__).resolve().parents[1] / "tests" / "production_profile.json"

# Rows pulled per table to decide a column's kind and to measure text and
# numeric spread. Categorical VALUE SETS are not sampled - they are counted
# exactly (see _categorical), because a rare value missed by a sample is
# precisely the shape no fixture ever produced.
SAMPLE_ROWS = 4000

# A column is categorical when it has few enough short distinct values that
# writing them all down is honest. Past this it is described, not quoted.
MAX_CATEGORIES = 60
MAX_CATEGORY_LEN = 96

# Never quote these verbatim, whatever their cardinality. Production has ONE
# user, so cardinality alone would happily record that person's email address
# as a category with weight 1.0.
#
# Spelled out per column rather than by column-name pattern. A pattern wide
# enough to catch `users.name` also catches `sources.name`, and suppressing
# that one would quietly delete the structure the analytics tests are built on
# (jobs.source values with no row in `sources`). Both mistakes are silent, so
# the list is the one that is auditable.
IDENTIFYING = frozenset(
    {
        "users.sub",
        "users.email",
        "users.name",
        "user_settings.identities",
        "user_settings.digest_token",
        "user_settings.api_key_enc",
        "user_settings.prefs",
        "user_settings.column_layout",
        "user_settings.background",
        "user_oauth_tokens.account_email",
        "user_oauth_tokens.refresh_token_enc",
        "user_oauth_tokens.access_token_enc",
        "email_messages.from_email",
        "email_messages.from_name",
        "email_messages.to_emails",
        "email_messages.subject",
        "email_messages.body_text",
        "email_messages.body_html",
        "email_messages.thread_topic",
        "email_messages.headers",
        "email_messages.provider_message_id",
        "email_messages.provider_thread_id",
        "email_events.detail",
        "application_matches.rationale",
        "applications.company_name",
        "applications.title",
        "user_jobs.notes",
        "user_jobs.recruiter",
        "user_jobs.connection1",
        "user_jobs.connection2",
        "user_jobs.documents",
        "reports.message",
        "reports.resolution_note",
        "reports.corrections",
        "source_requests.note",
        "source_requests.resolution_note",
        "user_filters.prompt",
        "user_filters.name",
        "filter_presets.prompt",
        "ai_prompts.instructions",
        "ai_prompt_samples.output",
        # Machine hostnames. Low cardinality and not secret, but one of them
        # is a laptop named after its owner, and the corpus does not need real
        # ones to exercise "which worker holds this task".
        "ai_queries.worker",
        "tasks.worker",
        "worker_status.name",
    }
)

# Columns whose value set only makes sense conditioned on another column.
#
# ai_queries.reason has 20,151 distinct values under check_type='custom' - free
# text a model wrote - and exactly three under 'content': 'scraped', 'ats
# text', 'content cached'. Those three are the shape the ATS-collapse detector
# divides by, and the shape no fixture ever produced. Measured unconditionally
# the column is "long free text" and the detector's input vanishes from the
# corpus; measured per check_type it is reproducible.
PARTITIONED = {"ai_queries.reason": "check_type"}

# Columns whose value is decided by the corpus's own structure rather than by
# production's distribution: the text keys that join tables the schema does not
# declare a foreign key between, and the hashes and ciphertexts that have to be
# recomputed to be coherent.
#
# Measuring these would be worse than useless. `ai_queries.prompt_hash` would
# be recorded as production's sixty filter hashes, which no generated filter
# has, so every custom verdict in the corpus would match nothing and every
# board would be empty - and the drift check would then cry every time a real
# user renamed a filter.
#
# `tests/corpus.py` is where each of them actually gets its value, and
# `tests/test_corpus.py` fails if this list and that file stop agreeing.
STRUCTURAL = frozenset(
    {
        "jobs.url",
        "jobs.raw_url",
        "ai_queries.url",
        "ai_queries.prompt_hash",
        "ai_queries.batch_id",
        "ai_batches.provider_batch_id",
        "ai_prompts.prompt_hash",
        "ai_prompt_samples.custom_id",
        "job_skills.url",
        "job_requirements.url",
        "job_requirements.content_hash",
        "job_embeddings.url",
        "job_embeddings.content_hash",
        "user_filters.prompt_hash",
        "user_sources.source",
        "tasks.dedupe_key",
        "tasks.parent_id",
        # Row references the schema declares no foreign key for. They are ids,
        # so their "distribution" is whatever the sequence has reached: two
        # runs a minute apart disagreed about worker_status.current_task_id,
        # because a worker had picked up a task in between.
        "worker_status.current_task_id",
        "ai_batches.task_id",
        "job_requirements.content_row_id",
        "job_embeddings.content_row_id",
        "email_messages.provider_message_id",
        "email_messages.provider_thread_id",
    }
)

# Belt and braces over the list above: a value that looks like a person or a
# secret is never quoted, whatever column it came out of. This is what covers
# the column nobody thought to add.
_LOOKS_PERSONAL = re.compile(
    r"[\w.+-]+@[\w-]+\.\w+"  # an email address
    r"|https?://"  # a URL, which carries a company and a posting
    # A token or an opaque provider id: long, and mixing case with digits.
    # Length alone was the first spelling and it rejected
    # 'when_dropped_as_not_job_related', a settings key that is nothing but a
    # sentence with underscores - so a real measured value was being dropped
    # for looking like a secret.
    r"|\b(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{20,}\b"
    r"|\b[0-9a-f]{32,}\b"  # a hex digest
)

# Columns whose length is measured but whose text is never transferred: the
# large text columns are ~80% of the database and pulling them to profile them
# would move hundreds of megabytes across the WAN for a number.
_LENGTH_ONLY = {"input_content", "instructions", "parsed_json", "body_text", "body_html", "prompt"}

# Not data. alembic_version is written by the migration that builds the schema,
# and a corpus that inserted into it would claim a revision it is not at.
SKIP_TABLES = {"alembic_version"}

QUANTILES = (0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0)


def _identifying(table: str, column: str) -> bool:
    return f"{table}.{column}" in IDENTIFYING


def _quantiles(values: list[float]) -> list[float]:
    """Order statistics rather than a mean and a sigma, because the shapes that
    break things live in the tails: the weekly-pay comp figure is the 0th
    percentile of comp_min, and no summary that averages it survives."""
    ordered = sorted(values)
    if not ordered:
        return []
    return [ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))] for q in QUANTILES]


def _charclass(text: str) -> str:
    """A string's alphabet, not its content. 'Acme Corp' and 'Beta Ltd' both
    reduce to 'Aa ', so a generator can produce something the same shape
    without the profile carrying either name."""
    classes = set()
    for ch in text[:200]:
        if ch.isupper():
            classes.add("A")
        elif ch.islower():
            classes.add("a")
        elif ch.isdigit():
            classes.add("9")
        elif ch.isspace():
            classes.add(" ")
        else:
            classes.add("-")
    return "".join(sorted(classes))


def _column_kind(data_type: str) -> str:
    if data_type in ("bigint", "integer", "smallint", "numeric", "double precision", "real"):
        return "numeric"
    if data_type == "boolean":
        return "boolean"
    if data_type.startswith("timestamp") or data_type == "date":
        return "timestamp"
    if data_type == "ARRAY":
        return "array"
    if data_type == "jsonb":
        return "json"
    if data_type == "bytea":
        return "bytes"
    if data_type == "USER-DEFINED":
        return "opaque"
    return "text"


def _select_expr(column: str, kind: str) -> str:
    """What to pull for a column. Large text becomes its own length, so the
    profile costs a number instead of a megabyte."""
    quoted = f'"{column}"'
    if kind == "opaque":
        return f"NULL AS {quoted}"
    if kind == "bytes" or column in _LENGTH_ONLY:
        return f"length({quoted})::bigint AS {quoted}"
    if kind == "text":
        return f"left({quoted}, 200) AS {quoted}"
    return quoted


def _key_columns(conn: psycopg.Connection, table: str) -> set[str]:
    """Columns whose value the corpus decides for itself, not the profile.

    Identity columns and foreign keys: production's `users.id` is the single
    value 1, which as a "distribution" would mean a corpus with one user and a
    drift alarm the moment a second person signs up.

    NOT every primary key. `sources.name` is a natural text key whose sixteen
    values are the structure the analytics tests are built on, and dropping it
    for being a key would take the measurement out of the measured corpus.
    """
    keys = {
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "AND (is_identity = 'YES' OR column_default LIKE 'nextval%%')",
            (table,),
        ).fetchall()
    }
    keys |= {
        r["column_name"]
        for r in conn.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.table_schema = tc.table_schema
            WHERE tc.table_schema = 'public' AND tc.table_name = %s
              AND tc.constraint_type = 'FOREIGN KEY'
            """,
            (table,),
        ).fetchall()
    }
    prefix = f"{table}."
    return keys | {c[len(prefix) :] for c in STRUCTURAL if c.startswith(prefix)}


def _categorical(
    conn: psycopg.Connection, table: str, column: str, where: str = ""
) -> dict[str, float] | None:
    """Exact value frequencies, or None when the column is not categorical.

    Counted over the whole table rather than the sample: the value that breaks
    a consumer is usually the rare one, and a sample is exactly the instrument
    that cannot see it.
    """
    rows = conn.execute(
        f'SELECT "{column}"::text AS v, count(*) AS n FROM "{table}" '
        f'WHERE "{column}" IS NOT NULL {where} GROUP BY 1 ORDER BY 2 DESC '
        f"LIMIT {MAX_CATEGORIES + 1}"
    ).fetchall()
    if len(rows) > MAX_CATEGORIES or not rows:
        return None
    if any(len(r["v"]) > MAX_CATEGORY_LEN or _LOOKS_PERSONAL.search(r["v"]) for r in rows):
        return None
    total = sum(r["n"] for r in rows)
    return {r["v"]: round(r["n"] / total, 6) for r in rows}


def _exact(
    conn: psycopg.Connection, table: str, columns: list[tuple[str, str]], rows_total: int
) -> dict[str, dict[str, Any]]:
    """Null counts and numeric extremes over the WHOLE table, in one query.

    Everything `drift()` compares has to be exact. Measured from the sample,
    these move on their own between two runs minutes apart: a column with a
    0.1% null rate shows zero nulls in one 4000-row sample and some in the
    next, and a sampled maximum is below the real one nearly always. The first
    run of --check against production reported 18 shapes as drift, and 18 of
    them were the sampler talking to itself. A check that fails nightly for no
    reason gets muted within a fortnight, and then the real one is missed too.

    The sample is still what describes shape - lengths, alphabets, the middle
    of a distribution. It is no longer what decides whether anything changed.
    """
    parts: list[str] = []
    for name, kind in columns:
        if kind == "opaque":
            continue
        parts.append(f'count("{name}") AS "n_{name}"')
        if kind == "numeric":
            parts.append(f'min("{name}")::float8 AS "lo_{name}"')
            parts.append(f'max("{name}")::float8 AS "hi_{name}"')
    if not parts or not rows_total:
        return {}
    row = conn.execute(f'SELECT {", ".join(parts)} FROM "{table}"').fetchone()
    assert row is not None
    out: dict[str, dict[str, Any]] = {}
    for name, kind in columns:
        if kind == "opaque":
            continue
        stats: dict[str, Any] = {"null_rate": round(1 - row[f"n_{name}"] / rows_total, 6)}
        if kind == "numeric" and row[f"n_{name}"]:
            stats["lo"], stats["hi"] = row[f"lo_{name}"], row[f"hi_{name}"]
        out[name] = stats
    return out


def _array_elements(conn: psycopg.Connection, table: str, column: str) -> dict[str, float] | None:
    """Every distinct element an array column holds, counted exactly."""
    rows = conn.execute(
        f'SELECT e::text AS v, count(*) AS n FROM (SELECT unnest("{column}") AS e '
        f'FROM "{table}") s WHERE e IS NOT NULL GROUP BY 1 ORDER BY 2 DESC '
        f"LIMIT {MAX_CATEGORIES + 1}"
    ).fetchall()
    if not rows or len(rows) > MAX_CATEGORIES:
        return None
    if any(len(r["v"]) > MAX_CATEGORY_LEN or _LOOKS_PERSONAL.search(r["v"]) for r in rows):
        return None
    total = sum(r["n"] for r in rows)
    return {r["v"]: round(r["n"] / total, 6) for r in rows}


def _json_documents(conn: psycopg.Connection, table: str, column: str) -> list[str] | None:
    """Every distinct document, when there are few and they are small.

    A generated user_settings.criteria has to be something criteria.params can
    actually read, and inventing one is how a corpus stops being a measurement.
    """
    rows = conn.execute(
        f'SELECT "{column}"::text AS v FROM "{table}" WHERE "{column}" IS NOT NULL '
        f"GROUP BY 1 LIMIT 21"
    ).fetchall()
    if not rows or len(rows) > 20:
        return None
    documents = sorted(json.dumps(json.loads(r["v"]), sort_keys=True) for r in rows)
    if any(len(d) > 400 or _LOOKS_PERSONAL.search(d) for d in documents):
        return None
    return documents


def _json_keysets(conn: psycopg.Connection, table: str, column: str) -> dict[str, float]:
    rows = conn.execute(
        f"""
        SELECT CASE WHEN jsonb_typeof("{column}") = 'object'
                    THEN (SELECT COALESCE(string_agg(k, ',' ORDER BY k), '')
                          FROM jsonb_object_keys("{column}") k)
                    ELSE '<' || jsonb_typeof("{column}") || '>' END AS v,
               count(*) AS n
        FROM "{table}" WHERE "{column}" IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT {MAX_CATEGORIES}
        """
    ).fetchall()
    total = sum(r["n"] for r in rows) or 1
    return {r["v"]: round(r["n"] / total, 6) for r in rows}


def _free_text_shape(values: list[Any]) -> dict[str, Any]:
    """A text column described rather than quoted: how long, over what
    alphabet. Enough for a generator to produce something the same shape, and
    for drift to notice when the alphabet gains a class."""
    strings = [v for v in values if isinstance(v, str)]
    return {
        "kind": "text",
        "null_rate": 0.0,
        "lengths": [int(q) for q in _quantiles([float(len(s)) for s in strings])],
        "binary": False,
        "charclasses": sorted({_charclass(s) for s in strings}),
    }


def _profile_column(
    conn: psycopg.Connection,
    table: str,
    column: str,
    kind: str,
    values: list[Any],
    *,
    is_key: bool = False,
    exact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exact = exact or {}
    present = [v for v in values if v is not None]
    out: dict[str, Any] = {
        "kind": kind,
        # Exact when there is one, because drift() compares this. The sampled
        # figure is only a fallback for a column no aggregate covers.
        "null_rate": exact.get(
            "null_rate", round(1 - len(present) / len(values), 6) if values else 1.0
        ),
    }

    if is_key:
        # Only whether it can be absent. The value is the generator's to choose
        # and production's is meaningless outside production.
        out["kind"] = "key"
        return out

    partition = PARTITIONED.get(f"{table}.{column}")
    if partition is not None:
        groups = conn.execute(
            f'SELECT DISTINCT "{partition}"::text AS v FROM "{table}" '
            f'WHERE "{partition}" IS NOT NULL ORDER BY 1 LIMIT {MAX_CATEGORIES}'
        ).fetchall()
        out["kind"] = "partitioned"
        out["on"] = partition
        out["parts"] = {}
        for group in groups:
            key = group["v"].replace("'", "''")
            categories = _categorical(conn, table, column, f"AND \"{partition}\"::text = '{key}'")
            out["parts"][group["v"]] = (
                {"kind": "categorical", "null_rate": 0.0, "values": categories}
                if categories is not None
                else _free_text_shape(present)
            )
        return out

    if kind in ("text", "numeric") and not _identifying(table, column):
        categories = _categorical(conn, table, column)
        if categories is not None:
            out["kind"] = "categorical"
            out["values"] = categories
            return out

    if kind == "boolean":
        out["true_rate"] = round(sum(1 for v in present if v) / len(present), 6) if present else 0.0
    elif kind == "numeric":
        quantiles = _quantiles([float(v) for v in present])
        # Ends exact, middle sampled. The ends are what drift() compares and
        # what the generator has to be able to reach: comp_min's floor is the
        # weekly-pay figure, and a sampled minimum misses it most runs.
        if quantiles and "lo" in exact:
            quantiles = [exact["lo"], *quantiles[1:-1], exact["hi"]]
        out["quantiles"] = sorted(quantiles)
    elif kind == "timestamp":
        now = datetime.datetime.now(tz=datetime.UTC)
        ages, naive = [], 0
        for v in present:
            stamp = (
                datetime.datetime.combine(v, datetime.time(), tzinfo=datetime.UTC)
                if isinstance(v, datetime.date) and not isinstance(v, datetime.datetime)
                else v
            )
            if stamp.tzinfo is None:
                naive += 1
                stamp = stamp.replace(tzinfo=datetime.UTC)
            ages.append((now - stamp).total_seconds() / 86400)
        out["age_days"] = [round(q, 3) for q in _quantiles(ages)]
        out["naive_rate"] = round(naive / len(present), 6) if present else 0.0
    elif kind in ("text", "bytes"):
        # _LENGTH_ONLY and bytea arrive as an integer length already.
        lengths = [float(v) if isinstance(v, int) else float(len(v)) for v in present]
        out["kind"] = "text"
        out["lengths"] = [int(q) for q in _quantiles(lengths)]
        out["binary"] = kind == "bytes"
        if column not in _LENGTH_ONLY and kind == "text":
            out["charclasses"] = sorted({_charclass(v) for v in present if isinstance(v, str)})
    elif kind == "array":
        out["lengths"] = [int(q) for q in _quantiles([float(len(v)) for v in present])]
        if not _identifying(table, column):
            elements = _array_elements(conn, table, column)
            if elements:
                out["elements"] = elements
    elif kind == "json":
        out["keysets"] = _json_keysets(conn, table, column)
        if not _identifying(table, column):
            documents = _json_documents(conn, table, column)
            if documents:
                out["documents"] = documents
    return out


def measure(url: str) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "measured_at": datetime.datetime.now(tz=datetime.UTC).isoformat(),
        "tables": {},
    }
    with psycopg.connect(url, row_factory=dict_row) as conn:
        conn.execute("SET default_transaction_read_only = on")
        tables = [
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"
            ).fetchall()
            if r["table_name"] not in SKIP_TABLES
        ]
        for table in tables:
            columns = [
                (r["column_name"], _column_kind(r["data_type"]))
                for r in conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
                    (table,),
                ).fetchall()
            ]
            count = conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()
            rows_total = int(count["n"]) if count else 0
            selects = ", ".join(_select_expr(c, k) for c, k in columns)
            # TABLESAMPLE rather than LIMIT once the table is big: a plain
            # LIMIT returns one physical page, which is whatever was written
            # most recently, and would hide every shape that has stopped being
            # written. Percentage is doubled so the LIMIT, not the sampler's
            # variance, is what decides the size.
            if rows_total > SAMPLE_ROWS * 2:
                pct = min(100.0, 200.0 * SAMPLE_ROWS / rows_total)
                query = (
                    f'SELECT {selects} FROM "{table}" TABLESAMPLE SYSTEM ({pct:.4f}) '
                    f"LIMIT {SAMPLE_ROWS}"
                )
            else:
                query = f'SELECT {selects} FROM "{table}" LIMIT {SAMPLE_ROWS}'
            sample = conn.execute(query).fetchall()
            keys = _key_columns(conn, table)
            exact = _exact(conn, table, columns, rows_total)
            profile["tables"][table] = {
                "rows": rows_total,
                "sampled": len(sample),
                "columns": {
                    column: _profile_column(
                        conn,
                        table,
                        column,
                        kind,
                        [r[column] for r in sample],
                        is_key=column in keys,
                        exact=exact.get(column),
                    )
                    for column, kind in columns
                },
            }
            print(f"  {table}: {rows_total} rows, {len(columns)} columns", file=sys.stderr)
    return profile


def drift(recorded: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Every way production now holds a shape the generator cannot produce.

    Only that direction. A category the profile has and production has stopped
    writing is not drift - the corpus keeping a shape production dropped is
    harmless, and failing on it would train people to re-measure reflexively
    until the check means nothing.
    """
    out: list[str] = []
    for table, now in current["tables"].items():
        was = recorded["tables"].get(table)
        if was is None:
            out.append(f"{table}: table exists in production and not in the profile")
            continue
        for column, shape in now["columns"].items():
            before = was["columns"].get(column)
            where = f"{table}.{column}"
            if before is None:
                out.append(f"{where}: column exists in production and not in the profile")
                continue
            out.extend(_shape_drift(where, before, shape))
    return out


def _outside(value: float, bound: float, *, upper: bool) -> bool:
    """Whether a range end has moved far enough to matter, at 10% of itself.

    Exactly equal is too strict on anything that accumulates. Token counts and
    costs creep past their old maximum every few days, and a check that fires
    on that gets muted, taking the case it exists for with it: comp_min's
    floor dropping from 22,256 to a weekly wage is a 97% move and still
    caught.
    """
    slack = abs(bound) * 0.1
    return value > bound + slack if upper else value < bound - slack


def _shape_drift(where: str, before: dict[str, Any], shape: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if before["kind"] != shape["kind"]:
        return [f"{where}: was {before['kind']}, is now {shape['kind']}"]
    if shape["kind"] == "key":
        return out
    if shape["null_rate"] > 0 and before["null_rate"] == 0:
        out.append(f"{where}: now holds nulls; the corpus never generates one")
    if shape["kind"] == "categorical":
        new = set(shape["values"]) - set(before["values"])
        if new:
            out.append(f"{where}: values no corpus row can hold: {sorted(new)}")
    elif shape["kind"] == "partitioned":
        for part, sub in shape["parts"].items():
            was = before["parts"].get(part)
            if was is None:
                out.append(f"{where}: a {before['on']}={part!r} the profile has never seen")
            else:
                out.extend(_shape_drift(f"{where}[{before['on']}={part}]", was, sub))
    elif shape["kind"] == "numeric" and shape.get("quantiles") and before.get("quantiles"):
        lo, hi = before["quantiles"][0], before["quantiles"][-1]
        now_lo, now_hi = shape["quantiles"][0], shape["quantiles"][-1]
        if _outside(now_lo, lo, upper=False) or _outside(now_hi, hi, upper=True):
            out.append(
                f"{where}: range moved to [{now_lo}, {now_hi}], profile has [{lo}, {hi}]"
            )
    elif shape["kind"] == "timestamp" and shape.get("naive_rate", 0) > before.get("naive_rate", 0):
        out.append(
            f"{where}: {shape['naive_rate']:.1%} of values are naive local time; "
            "every window query over this column is off by the writer's offset"
        )
    # Text carries only its null_rate into this comparison. Character classes
    # and lengths come out of a 4000-row sample, so they differ between two
    # runs of the same minute, and a longer string or a new punctuation mark
    # is not a shape the corpus fails to reach anyway.
    elif shape["kind"] == "array" and shape.get("elements"):
        new = set(shape["elements"]) - set(before.get("elements", {}))
        if new:
            out.append(f"{where}: array elements not in the profile: {sorted(new)}")
    elif shape["kind"] == "json" and shape.get("keysets"):
        new = set(shape["keysets"]) - set(before.get("keysets", {}))
        if new:
            out.append(f"{where}: json shapes not in the profile: {sorted(new)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="compare production against the committed profile and fail on drift",
    )
    args = ap.parse_args()

    dotenv.load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    print("measuring production...", file=sys.stderr)
    current = measure(url)

    if not args.check:
        PROFILE_PATH.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        print(f"\nwrote {PROFILE_PATH}")
        return 0

    findings = drift(json.loads(PROFILE_PATH.read_text()), current)
    if not findings:
        print("\nproduction still matches the profile.")
        return 0
    print(f"\nproduction has {len(findings)} shape(s) the generated corpus cannot produce:\n")
    for line in findings:
        print(f"  - {line}")
    print(
        "\nRe-measure and commit the profile, then check whether the corpus - and the "
        "tests reading it - still cover what it now says:\n"
        "  set -a && . ./.env && set +a && python scripts/measure_profile.py"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
