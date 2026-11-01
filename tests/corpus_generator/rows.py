from __future__ import annotations

import datetime
import json
import random
from typing import Any

from api import db
from tests.corpus_generator.config import (
    _DIGITS,
    _LETTERS,
    _PUNCT,
    _UPPER,
    HOT_SHARE,
    HOT_SLICE,
    LINKS,
    MIN_ROWS,
    SEED,
)
from tests.corpus_generator.schema import _Schema


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
            length = int(self._from_quantiles(lengths))
        classes = shape.get("charclasses") or ["Aa "]
        alphabet = ""
        for symbol in self.rng.choice(classes) or "a":
            alphabet += {"A": _UPPER, "a": _LETTERS, "9": _DIGITS, " ": " ", "-": _PUNCT}[symbol]
        # choices() in one call, not choice() per character. The profile keeps
        # production's real lengths, and page_fetches.content reaches 32k
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
            # The value's type comes from the profile, not from convenience.
            # Every key used to get a random string, so tasks.progress read
            # {"done": "y1e9e55hf1iz"} where production has {"done": 5}, and
            # health.py's (progress->>'total')::int failed on every row: a real
            # detector that could not run against the data standing in for
            # production. A column profiled before value_types existed has no
            # types recorded, and those keys keep the old behaviour.
            types = shape.get("value_types") or {}
            return db.jsonb({k: self._json_value(types.get(k)) for k in keys})
        if kind == "opaque":
            # A type the profile cannot describe (pgvector). If the column is
            # NOT NULL, _specialise has to fill it; nothing here can.
            return None
        if shape.get("binary"):
            return self._text({"lengths": shape.get("lengths") or [16]}).encode()
        return self._text(shape)

    def _json_value(self, jsonb_type: str | None) -> Any:
        """One value of the type production holds for this key."""
        if jsonb_type == "number":
            return self.rng.randint(0, 5000)
        if jsonb_type == "boolean":
            return self.rng.random() < 0.5
        if jsonb_type == "null":
            return None
        if jsonb_type == "array":
            return [self._text({"lengths": [4, 10], "charclasses": ["a9"]}) for _ in range(2)]
        if jsonb_type == "object":
            return {"k": self._text({"lengths": [4, 10], "charclasses": ["a9"]})}
        return self._text({"lengths": [6, 14], "charclasses": ["a9"]})

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
        n = max(MIN_ROWS, round(recorded * self.scale))
        # A table keyed by an undeclared link to its parent (job_comp.url)
        # holds at most one row per parent. Its count can outgrow the parent's
        # when the two were measured at different times: job_comp was measured
        # on 2026-10-10 against a jobs count from 2026-09-04.
        if len(pk) == 1 and (parent := LINKS.get((table, pk[0]))):
            n = min(n, len(self.pools.get(parent, [])))
        return n

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
            # A measured null_rate is production's, and the LOCAL schema is
            # what the insert has to satisfy. They can disagree - a migration
            # that added NOT NULL after the profile was taken, or a null_rate
            # measured a hair above zero - and the corpus must not put a NULL
            # into a NOT NULL column and die mid-build.
            nullable = column["is_nullable"] == "YES"
            null_rate = shape["null_rate"] if nullable else 0.0
            # Categoricals get quotas rather than draws, so no measured value
            # is lost to chance.
            if shape["kind"] == "categorical":
                nulls = [self.rng.random() < null_rate for _ in rows]
                picks = self._weighted(shape["values"], max(1, sum(1 for x in nulls if not x)))
                it = iter(picks)
                for row, is_null in zip(rows, nulls, strict=True):
                    row[name] = None if is_null else self._cast(next(it, picks[0]), column)
                continue
            for row in rows:
                if shape["kind"] != "partitioned" and self.rng.random() < null_rate:
                    row[name] = None
                else:
                    row[name] = self._scalar(shape, column, row)
            if not nullable:
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
        # Postgres names an array type after its element: text[] is _text. A
        # synthesised string is not a valid array literal, so a NOT NULL array
        # column the profile does not describe took the text branch below and
        # failed the insert with "malformed array literal". Empty is the right
        # default for the ones this hits: they are filters and tag lists whose
        # absence means "no filter", not "unknown".
        if column["udt_name"].startswith("_"):
            return []
        # Same shape of failure for a NOT NULL jsonb column the profile has
        # not measured yet (user_settings.profile, 2026-09-07): the text
        # below is not a JSON literal. Empty is what the column defaults to.
        if column["udt_name"] in ("json", "jsonb"):
            return db.jsonb({})
        return f"{table}-{name}-{self._next()}"
