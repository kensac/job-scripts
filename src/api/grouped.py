"""Fold rows of one GROUP BY into the coarser cuts a page shows.

A page that cuts one table several ways used to scan it once per cut. One scan
grouped by every key the cuts need, folded here, returns the same numbers:
counts add, and a SUM of group SUMs is the SUM, because Postgres hands numeric
sums over as exact Decimals.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def fold(
    groups: list[dict[str, Any]],
    sums: tuple[str, ...],
    counts: tuple[str, ...] = (),
    mins: tuple[str, ...] = (),
    maxs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """`sums` keep SQL's NULL rule: all-NULL inputs sum to None, never 0,
    because a column nobody recorded is not a recorded zero. COALESCE is the
    caller's to apply, field by field, as its SQL did."""
    acc: dict[str, Any] = dict.fromkeys(sums + mins + maxs) | dict.fromkeys(counts, 0)
    for g in groups:
        for k in sums:
            if g[k] is not None:
                acc[k] = g[k] if acc[k] is None else acc[k] + g[k]
        for k in counts:
            acc[k] += g[k]
        for k in mins:
            if g[k] is not None and (acc[k] is None or g[k] < acc[k]):
                acc[k] = g[k]
        for k in maxs:
            if g[k] is not None and (acc[k] is None or g[k] > acc[k]):
                acc[k] = g[k]
    return acc


def by(groups: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    """Groups partitioned by one key's value, None included as its own
    bucket, as GROUP BY treats NULL."""
    out: dict[Any, list[dict[str, Any]]] = {}
    for g in groups:
        out.setdefault(g[key], []).append(g)
    return out


def shaped[Shape: BaseModel](shape: type[Shape], acc: dict[str, Any]) -> Shape:
    """A fold read into a response shape, taking only the fields it declares;
    one fold carries the measures of several shapes."""
    return shape(**{k: acc[k] for k in shape.model_fields if k in acc})
