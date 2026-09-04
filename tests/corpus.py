"""Build a test corpus shaped like production, holding none of its data.

`docs/agents/testing.md` prefers a copy of production over fabricated rows,
and it is right about why: a fixture cannot falsify the assumption it was
built from. This keeps that property and drops the two costs the copy cannot
shed - it cannot run on a pull request, because reaching it needs a production
credential, and it cannot contain a second user, because production has one.

The trick is that the rows are fabricated but their SHAPES are not. Every
value here comes out of `tests/production_profile.json`, which
`scripts/measure_profile.py` measures off production: which values a column
actually holds and how often, how often it is null, how long its strings run,
how far back its timestamps go. Nothing in this file encodes what anyone
BELIEVES the data looks like, and `measure_profile.py --check` fails on a
schedule when production grows a shape this generator cannot produce.

Three kinds of knowledge, kept apart on purpose:

  shape       measured, lives in the profile, never written by hand
  structure   the schema's own foreign keys, read at build time, plus LINKS
              below for the joins the schema does not declare
  invariants  NOT reproduced here. A corpus built to satisfy an assertion
              makes that assertion a tautology. Where a test's whole subject
              is an invariant only the live writer maintains, the test stays
              on real data and says so.

Build it with `build()`; the conftest fixture does that for anything marked
`corpus`.
"""

from __future__ import annotations

import datetime
import json
import os
import random
import string
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from api import db

PROFILE_PATH = Path(__file__).resolve().parent / "production_profile.json"

# Jobs is the anchor: every other table is scaled by the ratio production has
# to it, so the corpus keeps production's proportions. 2000 clears the >1000
# floor the analytics assertions need with room to spare, and the whole build
# stays inside a few seconds.
TARGET_JOBS = 2000

# A floor, because scaling everything off jobs shrinks the small tables to
# nothing: user_filters is 10 rows in production, and 10 x 0.038 is zero
# filters, which is a corpus where no board can ever materialise. The floor is
# four rows per user, so every user has filters, sources and a history.
MIN_ROWS = 4 * 5

# More than one, which is the entire class of behaviour the copy can never
# cover: production has a single user row and a single distinct user_id in
# user_jobs, so every isolation and cross-user-leak assertion over the copy is
# vacuously true.
USERS = 5

# Fixed, so a failure is reproducible from the commit alone. A corpus that
# reshuffled per run would turn any shape-sensitive bug into a flake.
SEED = 294

# Joins the schema does not declare. The catalog's spine is jobs.url TEXT, not
# jobs.id - verdicts, requirements, skills and embeddings all key on the URL -
# so a generator that followed only foreign keys would emit four large tables
# that join to nothing, and every assertion over them would pass on zero rows.
# That is the "conditional vacuity" failure docs/agents/testing.md names.
LINKS = {
    ("ai_queries", "url"): ("jobs", "url"),
    ("job_skills", "url"): ("jobs", "url"),
    ("job_requirements", "url"): ("jobs", "url"),
    ("job_embeddings", "url"): ("jobs", "url"),
    ("job_requirements", "content_row_id"): ("ai_queries", "id"),
    ("job_embeddings", "content_row_id"): ("ai_queries", "id"),
    # Verdicts are keyed to a filter by the HASH of its prompt, not by its id.
    # Drawn from anywhere else, no custom verdict would ever match an enabled
    # filter and every user's board would be permanently empty.
    ("ai_queries", "prompt_hash"): ("user_filters", "prompt_hash"),
    ("ai_queries", "batch_id"): ("ai_batches", "provider_batch_id"),
    ("ai_batches", "task_id"): ("tasks", "id"),
    ("tasks", "parent_id"): ("tasks", "id"),
    ("user_sources", "source"): ("sources", "name"),
    ("action_items", "resolved_by_event_id"): ("email_events", "id"),
}

# Half of every parent draw comes from a small hot slice of that parent.
#
# Two things need it. Without it, 5 users spread over 2000 jobs share nothing,
# and "two people looking at the same posting" - the case the whole multi-user
# product turns on - never occurs in the corpus. And verdicts drawn uniformly
# over 2000 urls give each url 1.7 of them, so no job ever accumulates the
# closed + clearance + custom set a board row requires, and every board comes
# out empty. Production correlates both ways because rows are written by a
# pipeline that revisits the same posting; a marginal distribution cannot say
# so, and this is where that structure is put back.
HOT_SLICE = 0.05
HOT_SHARE = 0.5

# Filled by the app's own seed rather than by the profile. Deliberately just
# these two: `sources` is seeded from configs.toml, which is not in the
# repository, so on CI the seed writes nothing and the profile's sixteen real
# source names are the only thing that puts a catalog there at all.
SEEDED = {"app_config", "group_budgets"}

# Never generated. alembic_version says which migration built the schema.
SKIP = {"alembic_version"}

_LETTERS = string.ascii_lowercase
_UPPER = string.ascii_uppercase
_DIGITS = string.digits
_PUNCT = "-_.,:/()&+"


class _Schema:
    """The live schema of the database being filled, read once."""

    def __init__(self) -> None:
        self.columns: dict[str, list[dict[str, Any]]] = {}
        self.identity: dict[str, set[str]] = {}
        self.pk: dict[str, list[str]] = {}
        self.fks: dict[tuple[str, str], tuple[str, str]] = {}
        self.unique: dict[str, set[str]] = {}

        for row in db.query(
            "SELECT table_name, column_name, data_type, udt_name, is_nullable, "
            "is_identity, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
        ):
            table = row["table_name"]
            self.columns.setdefault(table, []).append(row)
            if row["is_identity"] == "YES" or (row["column_default"] or "").startswith("nextval"):
                self.identity.setdefault(table, set()).add(row["column_name"])

        for row in db.query(
            """
            SELECT c.conrelid::regclass::text AS child, a.attname AS col,
                   c.confrelid::regclass::text AS parent, f.attname AS pcol,
                   c.contype AS kind
            FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY k(att, i) ON true
            LEFT JOIN unnest(c.confkey) WITH ORDINALITY fk(att, i) ON fk.i = k.i
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.att
            LEFT JOIN pg_attribute f ON f.attrelid = c.confrelid AND f.attnum = fk.att
            WHERE c.contype IN ('f', 'p', 'u')
              AND c.connamespace = 'public'::regnamespace
            """
        ):
            child = row["child"].replace("public.", "")
            if row["kind"] == "f":
                self.fks[(child, row["col"])] = (row["parent"].replace("public.", ""), row["pcol"])
            elif row["kind"] == "p":
                self.pk.setdefault(child, []).append(row["col"])
            else:
                self.unique.setdefault(child, set()).add(row["col"])

    def parent_of(self, table: str, column: str) -> tuple[str, str] | None:
        return self.fks.get((table, column)) or LINKS.get((table, column))


def _order(schema: _Schema, tables: list[str]) -> list[str]:
    """Tables in dependency order. A self-reference is not a cycle - the rows
    are inserted one batch at a time and can point at earlier ones."""
    done: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(
            t
            for t in remaining
            if not {
                parent
                for column in schema.columns[t]
                for parent, _ in [schema.parent_of(t, column["column_name"]) or ("", "")]
                if parent and parent != t and parent in remaining
            }
        )
        if not ready:  # a genuine cycle; take the smallest and let FKs be null
            ready = [sorted(remaining)[0]]
        done.extend(ready)
        remaining -= set(ready)
    return done


class _Generator:
    def __init__(self, profile: dict[str, Any], schema: _Schema, scale: float) -> None:
        self.profile = profile
        self.schema = schema
        self.scale = scale
        self.rng = random.Random(SEED)
        self.pools: dict[tuple[str, str], list[Any]] = {}
        self.counter = 0

    # -- value synthesis ---------------------------------------------------

    def _next(self) -> int:
        self.counter += 1
        return self.counter

    def _text(self, shape: dict[str, Any], length: int | None = None) -> str:
        lengths = shape.get("lengths") or [8]
        if length is None:
            length = self._from_quantiles(lengths)
        classes = shape.get("charclasses") or ["Aa "]
        alphabet = ""
        for symbol in self.rng.choice(classes) or "a":
            alphabet += {"A": _UPPER, "a": _LETTERS, "9": _DIGITS, " ": " ", "-": _PUNCT}[symbol]
        # choices() in one call, not choice() per character. The profile keeps
        # production's real lengths, and ai_queries.input_content reaches 32k
        # characters, so per-character drawing spent 62 of the build's 78
        # seconds inside random.choice alone.
        return "".join(self.rng.choices(alphabet, k=max(0, int(length))))

    def _from_quantiles(self, quantiles: list[float]) -> float:
        """Uniform inside a randomly chosen quantile band, so the tails are
        reached as often as the profile says they are. The extremes are
        returned outright sometimes, because comp's weekly-pay figure IS the
        0th percentile and a generator that only ever interpolates never
        produces it."""
        if not quantiles:
            return 0.0
        if len(quantiles) == 1:
            return quantiles[0]
        i = self.rng.randrange(len(quantiles) - 1)
        lo, hi = quantiles[i], quantiles[i + 1]
        return lo if lo == hi or self.rng.random() < 0.1 else self.rng.uniform(lo, hi)

    def _weighted(self, values: dict[str, float], n: int) -> list[str]:
        """n draws in which EVERY measured value appears at least once.

        Plain weighted sampling silently drops the rare ones - comp_basis
        'unspecified' is 0.06% of production and would appear 0.1 times in
        2000 rows - and the rare value is reliably the one that breaks a
        consumer. Quotas first, remainder by weight, then shuffled.
        """
        out: list[str] = []
        for value, weight in values.items():
            out.extend([value] * max(1, round(weight * n)))
        if len(out) > n:
            # Set one of each aside first, then trim the surplus by sampling,
            # so what gets dropped is a duplicate and never a whole value.
            keep = list(values)
            surplus = list(out)
            for value in keep:
                surplus.remove(value)
            out = keep + self.rng.sample(surplus, max(0, n - len(keep)))
        while len(out) < n:
            out.append(self.rng.choices(list(values), weights=list(values.values()))[0])
        self.rng.shuffle(out)
        return out[:n]

    def _cast(self, raw: str, column: dict[str, Any]) -> Any:
        udt = column["udt_name"]
        if udt in ("int2", "int4", "int8"):
            return int(float(raw))
        if udt in ("numeric", "float4", "float8"):
            return float(raw)
        if udt == "bool":
            return raw in ("true", "t", "True")
        return raw

    def _scalar(self, shape: dict[str, Any], column: dict[str, Any], row: dict[str, Any]) -> Any:
        kind = shape["kind"]
        if kind == "partitioned":
            part = shape["parts"].get(str(row.get(shape["on"])))
            return self._scalar(part, column, row) if part else None
        if kind == "categorical":
            return self._cast(
                self.rng.choices(list(shape["values"]), weights=list(shape["values"].values()))[0],
                column,
            )
        if kind == "boolean":
            return self.rng.random() < shape.get("true_rate", 0.5)
        if kind == "numeric":
            value = self._from_quantiles(shape.get("quantiles") or [0.0])
            return int(value) if column["udt_name"] in ("int2", "int4", "int8") else value
        if kind == "timestamp":
            age = self._from_quantiles(shape.get("age_days") or [0.0])
            stamp = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=age)
            return stamp.date() if column["udt_name"] == "date" else stamp
        if kind == "array":
            length = int(self._from_quantiles(shape.get("lengths") or [1]))
            elements = shape.get("elements")
            if elements:
                pool = list(elements)
                return [
                    self.rng.choices(pool, weights=list(elements.values()))[0]
                    for _ in range(length)
                ]
            return [self._text({"lengths": [4, 12], "charclasses": ["a"]}) for _ in range(length)]
        if kind == "json":
            documents = shape.get("documents")
            if documents:
                return db.jsonb(json.loads(self.rng.choice(documents)))
            keyset = self.rng.choice(list(shape.get("keysets") or {"": 1.0}))
            if keyset.startswith("<"):
                return db.jsonb([])
            keys = [k for k in keyset.split(",") if k]
            return db.jsonb(
                {k: self._text({"lengths": [6, 14], "charclasses": ["a9"]}) for k in keys}
            )
        if kind == "opaque":
            # A type the profile cannot describe (pgvector). If the column is
            # NOT NULL, _specialise has to fill it; nothing here can.
            return None
        if shape.get("binary"):
            return self._text({"lengths": shape.get("lengths") or [16]}).encode()
        return self._text(shape)

    # -- row generation ----------------------------------------------------

    def _row_count(self, table: str) -> int:
        recorded = self.profile["tables"].get(table, {}).get("rows", 0)
        pk = self.schema.pk.get(table, [])
        shapes = self.profile["tables"].get(table, {}).get("columns", {})

        # One row per parent when the primary key IS the foreign key:
        # user_settings and user_oauth_tokens are per-user rows, not a
        # population with its own size.
        if len(pk) == 1 and self.schema.fks.get((table, pk[0])):
            parent = self.schema.fks[(table, pk[0])]
            return len(self.pools.get(parent, []))
        # A categorical primary key IS its value set. Generating three rows for
        # a sixteen-value key would drop thirteen of the values the profile
        # went to the trouble of counting exactly.
        if len(pk) == 1 and shapes.get(pk[0], {}).get("kind") == "categorical":
            return len(shapes[pk[0]]["values"])
        if not recorded:
            return 0  # empty in production; the profile has no shapes to use
        return max(MIN_ROWS, round(recorded * self.scale))

    def load_pool(self, table: str, column: str) -> None:
        """Read the values another table can point at, and SHUFFLE them.

        The shuffle is not cosmetic. Pools were read in the column's own order,
        and jobs.url is 'https://corpus.invalid/<source>/<n>', so the hot slice
        - the first 5% - was every job from whichever source sorts first.
        Every swept job then belonged to one source, no user was subscribed to
        it, and materialisation produced zero board rows while every board test
        went on passing. Ordered by the RNG, the slice is a random subset of
        the catalog, which is what a slice was supposed to be.
        """
        values = [r[column] for r in db.query(f'SELECT "{column}" FROM {table} ORDER BY 1')]
        self.rng.shuffle(values)
        self.pools[(table, column)] = values

    def _draw(self, parent: tuple[str, str]) -> Any:
        pool = self.pools.get(parent) or []
        if not pool:
            return None
        if self.rng.random() < HOT_SHARE:
            return self.rng.choice(pool[: max(MIN_ROWS, int(len(pool) * HOT_SLICE))])
        return self.rng.choice(pool)

    def rows(self, table: str, n: int) -> list[dict[str, Any]]:
        columns = [
            c
            for c in self.schema.columns[table]
            if c["column_name"] not in self.schema.identity.get(table, set())
        ]
        shapes = self.profile["tables"].get(table, {}).get("columns", {})
        rows: list[dict[str, Any]] = [{} for _ in range(n)]

        # Structure first: keys and links do not come from the profile.
        pk = self.schema.pk.get(table, [])
        for column in columns:
            name = column["column_name"]
            parent = self.schema.parent_of(table, name)
            if parent is None:
                continue
            # A table whose primary key IS a foreign key holds one row per
            # parent - user_settings, user_oauth_tokens. Drawing at random
            # would collide and silently leave users without settings, which
            # reads downstream as "this user has no criteria" rather than as a
            # broken corpus.
            if pk == [name]:
                for row, value in zip(rows, self.pools.get(parent, []), strict=False):
                    row[name] = value
                continue
            nullable = column["is_nullable"] == "YES"
            rate = shapes.get(name, {}).get("null_rate", 0.0) if nullable else 0.0
            for row in rows:
                row[name] = None if self.rng.random() < rate else self._draw(parent)

        # Then shapes, partitioned columns last so the column they key on is
        # already decided.
        ordered = sorted(
            columns,
            key=lambda c: shapes.get(c["column_name"], {}).get("kind") == "partitioned",
        )
        for column in ordered:
            name = column["column_name"]
            if rows and name in rows[0]:
                continue
            shape = shapes.get(name)
            if shape is None or shape["kind"] == "key":
                for row in rows:
                    row[name] = self._unique_fallback(table, name, column)
                continue
            # Categoricals get quotas rather than draws, so no measured value
            # is lost to chance.
            if shape["kind"] == "categorical":
                nulls = [self.rng.random() < shape["null_rate"] for _ in rows]
                picks = self._weighted(shape["values"], max(1, sum(1 for x in nulls if not x)))
                it = iter(picks)
                for row, is_null in zip(rows, nulls, strict=True):
                    row[name] = None if is_null else self._cast(next(it, picks[0]), column)
                continue
            for row in rows:
                if shape["kind"] != "partitioned" and self.rng.random() < shape["null_rate"]:
                    row[name] = None
                else:
                    row[name] = self._scalar(shape, column, row)
            if column["is_nullable"] == "NO":
                for row in rows:
                    if row[name] is None:
                        row[name] = self._scalar(shape, column, row)
        return rows

    def _unique_fallback(self, table: str, name: str, column: dict[str, Any]) -> Any:
        """A column the profile describes as a key but the generator still has
        to fill: a unique text column with no parent, like users.sub."""
        if column["is_nullable"] == "YES" and name not in self.schema.pk.get(table, []):
            return None
        if column["udt_name"] in ("int2", "int4", "int8"):
            return self._next()
        return f"{table}-{name}-{self._next()}"


# --- the handful of columns whose value is a function of the corpus ---------
#
# Not shapes and not schema: values that must agree with ANOTHER value in the
# same corpus, or the row is incoherent whatever its distribution says. Each
# one is here because something reads the relationship, and the list is short
# on purpose - a corpus that reproduced every invariant would make the tests
# over it tautologies.


def _specialise(table: str, rows: list[dict[str, Any]], gen: _Generator) -> None:
    if table == "users":
        for i, row in enumerate(rows):
            row["sub"] = f"corpus-user-{i}"
            row["email"] = f"user{i}@corpus.invalid"
            row["name"] = f"Corpus User {i}"
            # Every group the app itself knows about, spread across the users,
            # plus the measured ones. /job-scripts aggregates BY group, and
            # over a population of one every such number is the same whether
            # the query is right or wrong.
            row["groups"] = sorted(
                {
                    ["infra-admins", "jobtracker-users-internal"][i % 2],
                    *row.get("groups", []),
                }
            )
    elif table == "jobs":
        for i, row in enumerate(rows):
            row["url"] = f"https://corpus.invalid/{row['source']}/{i}"
            row["raw_url"] = row["url"] + "?utm_source=corpus"
            lo, hi = row.get("comp_min"), row.get("comp_max")
            if lo is not None and hi is not None and lo > hi:
                row["comp_min"], row["comp_max"] = hi, lo
    elif table == "user_filters":
        from core.filters import build_custom_instructions, compute_prompt_hash

        for i, row in enumerate(rows):
            row["name"] = f"corpus filter {i}"
            # Distinct per filter, so distinct prompt_hashes: two enabled
            # filters sharing a hash makes the board's filter gate
            # unsatisfiable, and a corpus that shipped that would leave every
            # board empty for a reason nothing points at.
            kind = ["backend", "frontend", "data", "ml"][i % 4]
            row["prompt"] = f"must be a {kind} role, requirement {i}"
            # Computed with the shipping template, because a stored hash that
            # does not match one is a filter whose board silently empties.
            row["prompt_hash"] = compute_prompt_hash(
                build_custom_instructions(row["prompt"], row["on_ambiguous"])
            )
        # The measured rate is 10% enabled - production holds ten filters and
        # runs one - and drawn per row that leaves most users with none at all.
        # A user with no enabled filter gets no board, because materialisation
        # requires at least one, so drawing it independently would leave the
        # whole board path unreached in most runs and reached in some: a
        # corpus that is sometimes vacuous is worse than one that always is.
        # The rate is kept, the floor of one per user is added.
        by_user: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            by_user.setdefault(row["user_id"], []).append(row)
        rate = gen.profile["tables"]["user_filters"]["columns"]["enabled"]["true_rate"]
        for owned in by_user.values():
            for i, row in enumerate(owned):
                row["enabled"] = i < max(1, round(rate * len(owned)))
    elif table == "ai_batches":
        for i, row in enumerate(rows):
            row["provider_batch_id"] = f"batch_corpus_{i:08d}"
    elif table == "email_messages":
        for i, row in enumerate(rows):
            row["provider_message_id"] = f"corpus-msg-{i}"
            row["from_email"] = f"careers@company{i % 97}.invalid"
            row["from_name"] = f"Company {i % 97} Recruiting"
            row["subject"] = f"Your application to Company {i % 97}"
            row["to_emails"] = [f"user{row['user_id']}@corpus.invalid"]
    elif table == "user_oauth_tokens":
        from api import crypto

        for row in rows:
            # Real ciphertext under the suite's own key, so the paths that
            # decrypt a token are exercised rather than mocked. This answers
            # the ticket's open question: nothing about the encrypted material
            # forces those paths to stay mocked.
            row["refresh_token_enc"] = crypto.encrypt(f"corpus-refresh-{row['user_id']}")
            row["access_token_enc"] = crypto.encrypt(f"corpus-access-{row['user_id']}")
            row["provider"] = "google"
            row["account_email"] = f"user{row['user_id']}@corpus.invalid"
    elif table == "user_settings":
        from api import crypto

        for row in rows:
            row["api_key_enc"] = crypto.encrypt("corpus-api-key")
            row["digest_token"] = f"corpus-digest-{row['user_id']}"
    elif table == "ai_queries":
        _pair_verdicts_into_sweeps(rows, gen)
    elif table == "user_jobs":
        # A board row exists for one of two reasons, and they are not
        # interchangeable: the user TOUCHED the job, or materialisation added
        # it because the job passes every filter. Generating both kinds by
        # distribution would make "the board is not empty" true whether or not
        # materialisation ran at all, and the tests over the write-time
        # predicate would stop being able to fail.
        #
        # So the generator writes only touched rows, and every untouched row in
        # the corpus is one the application's own predicate put there.
        statuses = list(
            gen.profile["tables"]["user_jobs"]["columns"]["status"].get("values") or {"Applied": 1}
        )
        for i, row in enumerate(rows):
            row["status"] = row.get("status") or statuses[i % len(statuses)]
    elif table == "job_embeddings":
        from core.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL

        for row in rows:
            # pgvector will not take anything else, and the profile cannot
            # describe a vector without carrying one. The axis is jittered so
            # nearest-neighbour ordering is deterministic but not degenerate.
            vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
            vector[1] = gen.rng.random()
            row["embedding"] = str(vector)
            row["model"] = EMBEDDING_MODEL
            row["content_hash"] = f"corpus-{gen._next():08x}"
    elif table == "ai_prompts":
        for i, row in enumerate(rows):
            row["prompt_hash"] = f"corpusprompt{i:04d}"


def _pair_verdicts_into_sweeps(rows: list[dict[str, Any]], gen: _Generator) -> None:
    """Give some jobs a COMPLETE set of verdicts instead of a random handful.

    The pipeline does not evaluate one filter against one posting. It sweeps: a
    candidate job gets a closed verdict, a clearance verdict, and one custom
    verdict per enabled filter. Board membership needs all of them at once, so
    a corpus that draws (url, check_type, prompt_hash) independently produces
    2000 jobs each holding two unrelated verdicts and not one job that any
    board can contain.

    Measured, and worth writing down: with independent draws, materialisation
    inserted ZERO rows. Every board test still passed, over a user_jobs table
    the generator had filled by itself, which is exactly the vacuity this
    whole design has to avoid.

    This changes only the PAIRING. Row counts, the check_type mix and the
    status distribution are all still whatever the profile says; what is put
    back is the pipeline's own structure, which a per-column distribution
    cannot express.
    """
    urls = gen.pools.get(("jobs", "url")) or []
    hashes = [
        r["prompt_hash"]
        for r in db.query("SELECT prompt_hash FROM user_filters WHERE enabled ORDER BY id")
    ]
    if not urls or not hashes:
        return

    custom = [r for r in rows if r.get("check_type") == "custom"]
    # Self-sizing: as many swept jobs as there are custom verdicts to go round,
    # so every swept job gets a complete set and none gets a partial one.
    swept = urls[: max(1, len(custom) // len(hashes))]
    for i, row in enumerate(custom):
        row["url"] = swept[i // len(hashes)] if i // len(hashes) < len(swept) else row["url"]
        row["prompt_hash"] = hashes[i % len(hashes)]

    # The structural gates the same jobs have to clear. Reassigning existing
    # rows rather than adding any, so the check_type mix is untouched.
    for check in ("closed", "clearance"):
        gates = [r for r in rows if r.get("check_type") == check]
        for url, row in zip(swept, gates, strict=False):
            row["url"] = url


def _insert(table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    names = list(rows[0])
    collist = ", ".join(f'"{c}"' for c in names)
    placeholders = ", ".join(f"%({c})s" for c in names)
    sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    with db.pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)


def _parent_columns(schema: _Schema) -> dict[str, set[str]]:
    wanted: dict[str, set[str]] = {}
    for parent, column in list(schema.fks.values()) + list(LINKS.values()):
        wanted.setdefault(parent, set()).add(column)
    return wanted


def build(*, target_jobs: int = TARGET_JOBS) -> dict[str, int]:
    """Fill the current database with a corpus. Returns rows written per table.

    Destructive: everything is truncated first. The caller is the test suite,
    against a database whose name conftest has already refused to accept
    unless it is disposable.
    """
    _refuse_to_destroy_anything_that_matters()
    profile = json.loads(PROFILE_PATH.read_text())
    schema = _Schema()

    tables = [
        t for t in profile["tables"] if t not in SKIP and t in schema.columns and t not in SEEDED
    ]
    scale = target_jobs / max(1, profile["tables"]["jobs"]["rows"])
    gen = _Generator(profile, schema, scale)
    wanted = _parent_columns(schema)

    everything = [t for t in schema.columns if t not in SKIP]
    db.execute(f"TRUNCATE TABLE {', '.join(everything)} RESTART IDENTITY CASCADE")
    db.init_schema()  # re-seeds sources, source_groups, app_config, group_budgets

    for table in SEEDED:
        for column in wanted.get(table, set()):
            gen.load_pool(table, column)

    written: dict[str, int] = {}
    for table in _order(schema, tables):
        n = USERS if table == "users" else gen._row_count(table)
        rows = gen.rows(table, n)
        _specialise(table, rows, gen)
        _insert(table, rows)
        for column in wanted.get(table, set()):
            gen.load_pool(table, column)
        count = db.query_one(f"SELECT count(*) AS c FROM {table}")
        written[table] = int(count["c"]) if count else 0

    _materialise_boards()
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('testdb_corpus_seed', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (
            db.jsonb(
                {
                    "seed": SEED,
                    "profile_measured_at": profile["measured_at"],
                    "fingerprint": fingerprint(),
                }
            ),
        ),
    )
    return written


def _refuse_to_destroy_anything_that_matters() -> None:
    """Two ways build() could delete something it should not.

    The database NAME, because `make corpus` calls build() outside pytest and
    therefore outside conftest's own name gate. A mistyped DATABASE_URL there
    would TRUNCATE whatever it reached, with nothing in between.

    And a SYNCED COPY, because a full `pytest tests` pointed at one would have
    the corpus fixture quietly truncate the very rows the integration tests
    exist to read - then those tests would skip, having found no production
    data, and the run would be green. conftest already carries a note that
    this fixture silently did exactly that once.
    """
    name = (urlparse(os.environ["DATABASE_URL"]).path or "").lstrip("/")
    if not (name.endswith(("_test", "_ci")) or name.startswith("test_")):
        raise RuntimeError(
            f"refusing to build a corpus in {name!r}: building truncates every "
            "table. Name it *_test or *_ci."
        )
    if holds_real_data():
        raise RuntimeError(
            f"{name!r} holds a synced copy of production, and building the corpus "
            "would truncate it.\n"
            "Point the suite at a different database, or select only the tests "
            "that want the copy:\n"
            "    make integration"
        )


def _materialise_boards() -> None:
    """Run the application's OWN write-time board predicate over the corpus.

    Deliberately not a hand-written INSERT. The board is defined twice - as a
    read-time predicate in routers/jobs.py and a write-time one in
    tasks/board.py - and `test_the_two_visibility_predicates_agree` exists
    because they have already drifted. If the corpus materialised boards its
    own way, that test would compare the read path against the corpus instead
    of against the write path, and stop being able to see the drift at all.
    """
    from api.tasks.board import _materialize_passing

    for row in db.query("SELECT id FROM users ORDER BY id"):
        _materialize_passing(row["id"])


def fingerprint() -> str:
    """This generator plus the profile it reads, hashed.

    Presence alone is not enough to reuse a corpus. Editing the generator or
    re-measuring the profile leaves a database that still LOOKS built, so the
    suite reuses it and the change appears to have had no effect - which is a
    test that silently stopped being able to fail. Reproduced while checking
    exactly that: four deliberate breakages of the generator all still passed,
    against a corpus built before them.
    """
    import hashlib

    payload = Path(__file__).read_bytes() + PROFILE_PATH.read_bytes()
    return hashlib.sha256(payload).hexdigest()[:16]


def is_present() -> bool:
    row = db.query_one("SELECT value FROM app_config WHERE key = 'testdb_corpus_seed'")
    return bool(row) and row["value"].get("fingerprint") == fingerprint()


def holds_real_data() -> bool:
    """True when this database is a synced copy of production rather than a
    corpus. `sync_testdb.py` stamps when the copy was cut; `build()` stamps
    that it generated one. A database with neither is an empty scratch."""
    row = db.query_one("SELECT 1 AS t FROM app_config WHERE key = 'testdb_synced_at'")
    return bool(row) and not is_present()
