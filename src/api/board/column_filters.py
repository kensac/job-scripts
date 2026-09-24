"""Typed, parameterized refinements of the already-authorized board read."""

import datetime
import json
from decimal import Decimal
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class ColumnFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    operator: str
    value: str | None = Field(default=None, max_length=1000)


FilterKind = Literal["text", "date", "number", "boolean"]


class FilterField(BaseModel):
    key: str
    label: str
    kind: FilterKind
    operators: list[str]


_OPERATORS = {
    "text": ["contains", "not_contains", "equals", "not_equals", "is_empty", "is_not_empty"],
    "date": ["equals", "gte", "lte", "is_empty", "is_not_empty"],
    "number": ["equals", "gte", "lte", "is_empty", "is_not_empty"],
    "boolean": ["equals"],
}
_FIELDS: dict[str, tuple[str, FilterKind, str]] = {
    "company": ("Company", "text", "j.company"),
    "title": ("Role", "text", "j.title"),
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
    "comp_currency": ("Pay currency", "text", "j.comp_currency"),
    "comp_period": ("Pay period", "text", "j.comp_period"),
    "comp_min": ("Posted pay minimum", "number", "j.comp_min"),
    "comp_max": ("Posted pay maximum", "number", "j.comp_max"),
    "date_posted": ("Posted date (UTC)", "date", "(j.date_posted AT TIME ZONE 'UTC')::date"),
    "date_applied": ("Applied date", "date", "uj.date_applied"),
    "added_at": ("Added date (UTC)", "date", "(j.created_at AT TIME ZONE 'UTC')::date"),
    "active": ("Listed by source", "boolean", "j.active"),
}


def fields() -> list[FilterField]:
    return [
        FilterField(key=key, label=label, kind=kind, operators=_OPERATORS[kind])
        for key, (label, kind, _) in _FIELDS.items()
    ]


def compile_filters(raw: str | None) -> tuple[list[ColumnFilter], list[str], dict]:
    if not raw:
        return [], [], {}
    try:
        values = json.loads(raw)
        # Protocol bound on a query expression, not a processing-volume setting.
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError("Supply at most 32 column filters")
        rules = [ColumnFilter.model_validate(value) for value in values]
        clauses, params = [], {}
        for index, rule in enumerate(rules):
            if rule.field not in _FIELDS:
                raise ValueError(f"Unknown filter field: {rule.field}")
            _, kind, expression = _FIELDS[rule.field]
            if rule.operator not in _OPERATORS[kind]:
                raise ValueError(f"Unsupported operator for {rule.field}")
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
            name = f"column_filter_{index}"
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
                if not units.get("comp_currency") or units.get("comp_period") not in (
                    "year",
                    "month",
                    "week",
                    "day",
                    "hour",
                ):
                    raise ValueError("Pay comparisons require an exact currency and pay period")
            elif kind == "boolean":
                if rule.value not in ("true", "false"):
                    raise ValueError("Boolean filters accept true or false")
                value = rule.value == "true"
            if rule.operator in ("contains", "not_contains"):
                value = (
                    "%"
                    + rule.value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    + "%"
                )
                op = "ILIKE" if rule.operator == "contains" else "NOT ILIKE"
            else:
                op = {"equals": "=", "not_equals": "<>", "gte": ">=", "lte": "<="}[rule.operator]
            if kind == "text" and rule.operator in ("equals", "not_equals"):
                clauses.append(f"AND lower({expression}) {op} lower(%({name})s)")
            else:
                clauses.append(f"AND {expression} {op} %({name})s")
            params[name] = value
        return rules, clauses, params
    except (ValueError, ArithmeticError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
