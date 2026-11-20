"""Typed, parameterized refinements of the already-authorized board read."""

import datetime
import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api import db
from api.mail import applications
from api.problem import refuse
from core import catalog
from core.derivations.compensation import canonical_compensation_period


class ColumnFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    operator: str
    # A list operator (any_of, none_of) carries a JSON array here.
    value: str | None = Field(default=None, max_length=4000)


FilterKind = Literal["text", "date", "number", "boolean", "place"]


class FacetValue(BaseModel):
    value: str
    count: int


class FilterField(BaseModel):
    key: str
    label: str
    kind: FilterKind
    operators: list[str]


_LIST_OPERATORS = ("any_of", "none_of")
_OPERATORS = {
    "text": [
        "contains",
        "not_contains",
        "equals",
        "not_equals",
        "any_of",
        "none_of",
        "is_empty",
        "is_not_empty",
    ],
    "date": ["equals", "gte", "lte", "is_empty", "is_not_empty"],
    "number": ["equals", "gte", "lte", "is_empty", "is_not_empty"],
    "boolean": ["equals"],
    "place": list(_LIST_OPERATORS),
}
_FIELDS: dict[str, tuple[str, FilterKind, str]] = {
    # Trimmed so `equals` reads lower(btrim(company)), which idx_jobs_company_key
    # serves: untrimmed, an exact company on the 1.1M-row catalog walked the
    # primary key for 5 s; trimmed, 0.4 s (2026-10-15).
    "company": ("Company", "text", "btrim(j.company)"),
    "title": ("Role", "text", "j.title"),
    "place": ("Location", "place", ""),
    "locations": ("Location text", "text", "array_to_string(j.locations, ', ')"),
    "terms": ("Employment terms", "text", "array_to_string(j.terms, ', ')"),
    "source": ("Source", "text", "j.source"),
    "url": ("Posting URL", "text", "j.url"),
    "notes": ("Notes", "text", "uj.notes"),
    "recruiter": ("Recruiter", "text", "uj.recruiter"),
    "connection1": ("Connection 1", "text", "uj.connection1"),
    "connection2": ("Connection 2", "text", "uj.connection2"),
    "documents": ("Documents", "text", "uj.documents"),
    "size": ("Company size", "text", "uj.size"),
    "comp_currency": ("Pay currency", "text", "pay.comp_currency"),
    "comp_period": ("Pay period", "text", "pay.comp_period"),
    "comp_min": ("Posted pay minimum", "number", "pay.comp_min"),
    "comp_max": ("Posted pay maximum", "number", "pay.comp_max"),
    "date_posted": ("Posted date (UTC)", "date", "(j.date_posted AT TIME ZONE 'UTC')::date"),
    "date_applied": ("Applied date", "date", applications.applied_on("uj")),
    "added_at": ("Added date (UTC)", "date", "(j.created_at AT TIME ZONE 'UTC')::date"),
    "active": ("Listed by source", "boolean", catalog.IS_AVAILABLE.format(job="j")),
}


# The person's own row (`uj`), which only exists on a board read. The admin
# catalog has no person, so it offers everything else.
_BOARD_ONLY = frozenset(
    {"notes", "recruiter", "connection1", "connection2", "documents", "size", "date_applied"}
)


def _offered(catalog_only: bool) -> dict[str, tuple[str, FilterKind, str]]:
    return {k: v for k, v in _FIELDS.items() if not (catalog_only and k in _BOARD_ONLY)}


def fields(catalog_only: bool) -> list[FilterField]:
    return [
        FilterField(key=key, label=label, kind=kind, operators=_OPERATORS[kind])
        for key, (label, kind, _) in _offered(catalog_only).items()
    ]


def reads_pay(rules: list[ColumnFilter]) -> bool:
    """Whether a compiled filter names the posting's pay row (`pay`), so a
    read that does not select pay knows when it must join job_comp."""
    return any(_FIELDS[rule.field][2].startswith("pay.") for rule in rules)


def compile_filters(
    raw: str | None, catalog_only: bool
) -> tuple[list[ColumnFilter], list[str], dict]:
    if not raw:
        return [], [], {}
    try:
        values = json.loads(raw)
        # Protocol bound on a query expression, not a processing-volume setting.
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError("Supply at most 32 column filters")
        rules = [ColumnFilter.model_validate(value) for value in values]
        clauses, params = compile_rules(rules, catalog_only)
        return rules, clauses, params
    except (ValueError, ArithmeticError) as error:
        raise refuse(422, "INVALID_COLUMN_FILTER", str(error)) from error


# A place pick is "remote" or "COUNTRY[/REGION[/CITY]]", the levels of a
# `locations.places` entry. The facet (api.routers.job_board) serves the same
# spelling, so a pick is always one the board can count.
REMOTE = "remote"


def place_json(token: str) -> str:
    """The `places` entry a pick contains, as JSON for jsonb containment: a
    country pick matches every region and city in it."""
    country, region, city = ([*token.split("/", 2), "", ""])[:3]
    if len(country) != 2 or not country.isalpha():
        raise ValueError(f"Unknown place: {token}")
    entry = {"country": country.upper()}
    if region:
        entry["region"] = region.upper()
    if city:
        entry["city"] = city
    return json.dumps([entry])


def _list_value(rule: ColumnFilter) -> list[str]:
    try:
        values = json.loads(rule.value or "")
    except ValueError:
        values = None
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(v, str) and v.strip() for v in values)
    ):
        raise ValueError(f"{rule.field} {rule.operator} takes a JSON list of values")
    return [v.strip() for v in values]


def _place_clause(rule: ColumnFilter, name: str, params: dict) -> str:
    picks = _list_value(rule)
    params[f"{name}_remote"] = any(p.lower() == REMOTE for p in picks)
    params[f"{name}_places"] = [place_json(p) for p in picks if p.lower() != REMOTE]
    # Same join as the board criteria (api.board.criteria): a string not yet
    # classified has no row and matches nothing.
    match = f"""EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              JOIN locations l ON l.text = btrim(loc)
              WHERE (%({name}_remote)s AND l.remote)
                 OR l.places @> ANY(%({name}_places)s::text[]::jsonb[]))"""
    return f"AND {match}" if rule.operator == "any_of" else f"AND NOT {match}"


def compile_rules(rules: list[ColumnFilter], catalog_only: bool) -> tuple[list[str], dict]:
    """AND of the rules as SQL clauses over `j`, `uj` and `pay`. Raises
    ValueError for a rule the endpoint does not offer."""
    offered = _offered(catalog_only)
    clauses: list[str] = []
    params: dict = {}
    for index, rule in enumerate(rules):
        if rule.field not in offered:
            raise ValueError(f"Unknown filter field: {rule.field}")
        _, kind, expression = offered[rule.field]
        if rule.operator not in _OPERATORS[kind]:
            raise ValueError(f"Unsupported operator for {rule.field}")
        name = f"column_filter_{index}"
        if kind == "place":
            clauses.append(_place_clause(rule, name, params))
            continue
        if rule.operator in _LIST_OPERATORS:
            params[name] = [v.lower() for v in _list_value(rule)]
            match = f"lower({expression}) = ANY(%({name})s)"
            clauses.append(f"AND {match}" if rule.operator == "any_of" else f"AND NOT {match}")
            continue
        if rule.operator in ("is_empty", "is_not_empty"):
            if rule.value is not None:
                raise ValueError("Empty-value operators do not take a value")
            empty = (
                f"NULLIF(btrim({expression}), '') IS NULL"
                if kind == "text"
                else f"{expression} IS NULL"
            )
            clauses.append(
                f"AND ({empty})" if rule.operator == "is_empty" else f"AND NOT ({empty})"
            )
            continue
        if rule.value is None or not rule.value.strip():
            raise ValueError(f"A value is required for {rule.field}")
        value: object = rule.value
        if rule.operator in ("not_equals", "not_contains"):
            clauses.append(f"AND NULLIF(btrim({expression}), '') IS NOT NULL")
        if kind == "date":
            value = datetime.date.fromisoformat(rule.value)
        elif kind == "number":
            value = Decimal(rule.value)
            if not value.is_finite():
                raise ValueError("Pay must be a finite number")
            if abs(value) > Decimal("1e18"):
                raise ValueError("Pay value exceeds the supported numeric range")
            units = {r.field: r.value for r in rules if r.operator == "equals"}
            if (
                not units.get("comp_currency")
                or canonical_compensation_period(units.get("comp_period")) is None
            ):
                raise ValueError("Pay comparisons require an exact currency and pay period")
        elif rule.field == "comp_period" and rule.operator in ("equals", "not_equals"):
            value = canonical_compensation_period(rule.value) or rule.value
        elif kind == "boolean":
            if rule.value not in ("true", "false"):
                raise ValueError("Boolean filters accept true or false")
            value = rule.value == "true"
        if rule.operator in ("contains", "not_contains"):
            value = (
                "%" + rule.value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            op = "ILIKE" if rule.operator == "contains" else "NOT ILIKE"
        else:
            op = {"equals": "=", "not_equals": "<>", "gte": ">=", "lte": "<="}[rule.operator]
        if kind == "text" and rule.operator in ("equals", "not_equals"):
            clauses.append(f"AND lower({expression}) {op} lower(%({name})s)")
        else:
            clauses.append(f"AND {expression} {op} %({name})s")
        params[name] = value
    return clauses, params


def place_counts(board_sql: str, params: dict) -> list[FacetValue]:
    """Rows of `board_sql` (selecting `id, locations`) per place, at every
    level a classified location names, and the remote rows, most first. A
    value is a pick the place filter takes."""
    rows = db.query(
        f"""
        WITH b AS ({board_sql}),
        p AS (
            SELECT b.id, l.remote, pl->>'country' AS c, pl->>'region' AS r, pl->>'city' AS ci
            FROM b, unnest(b.locations) loc
            JOIN locations l ON l.text = btrim(loc)
            LEFT JOIN LATERAL jsonb_array_elements(l.places) pl ON TRUE
        )
        SELECT c, r, ci, GROUPING(r, ci) AS level, COUNT(DISTINCT id) AS n
        FROM p WHERE c IS NOT NULL GROUP BY GROUPING SETS ((c), (c, r), (c, r, ci))
        UNION ALL
        SELECT NULL, NULL, NULL, -1, COUNT(DISTINCT id) FROM p WHERE remote
        """,
        params,
    )
    out = []
    for row in rows:
        level, n = row["level"], row["n"]
        if level == -1:
            value = REMOTE if n else None
        elif level == 3:
            value = row["c"]
        elif level == 1:
            value = f"{row['c']}/{row['r']}" if row["r"] else None
        else:
            value = f"{row['c']}/{row['r'] or ''}/{row['ci']}" if row["ci"] else None
        if value:
            out.append(FacetValue(value=value, count=n))
    return sorted(out, key=lambda f: (-f.count, f.value))


def company_counts(board_sql: str, params: dict) -> list[FacetValue]:
    """Rows of `board_sql` (selecting `company`) per company, most first."""
    return db.query_as(
        FacetValue,
        f"""SELECT min(btrim(b.company)) AS value, COUNT(*) AS count FROM ({board_sql}) b
        WHERE btrim(b.company) <> '' GROUP BY lower(btrim(b.company)) ORDER BY 2 DESC, 1""",
        params,
    )
