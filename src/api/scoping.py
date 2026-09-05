"""User scoping for admin reads: one predicate per table, one parameter.

This is a multi-user application, and every admin list that returns rows a
person owns takes the same `user=<id>[,<id>]` and narrows rows, summaries
and totals to those users. The predicate for each table is spelled here and
nowhere else, bound to the one parameter name `user_ids`, so a handler
cannot scope one table one way and another table another way, and a new
list endpoint takes scoping by naming the table rather than by writing SQL.

Which tables own rows per user:
- users, reports, email_messages, api_usage: a user_id column.
- tasks (and batches through their task): user_id in the payload, on the
  kinds that run for a person; fleet work carries none and is out of every
  user's scope.
- ai_queries: no column. A custom filter verdict belongs to whoever owns the
  filter whose prompt produced it, so the scope is the user's prompt hashes.
  Closed, clearance and content rows are shared and belong to no one.

An endpoint's envelope says which parameters it filters on (`filterable`)
beside the echo of what was applied (`filters`), so a client renders a User
control from the former and never has to guess.
"""

from __future__ import annotations

from api import params as params_


def user_ids(value: str | None) -> list[int]:
    """The ids in `user=1,2`; anything that is not an integer is ignored
    rather than refused, the same way an unknown sort key is."""
    return [int(v) for v in params_.csv(value) if v.isdigit()]


def echo(ids: list[int]) -> list[str]:
    return [str(i) for i in ids]


# Every predicate binds %(user_ids)s, and the handler sets params["user_ids"].
def column(col: str) -> str:
    return f"{col} = ANY(%(user_ids)s)"


def task(alias: str = "") -> str:
    p = f"{alias}.payload" if alias else "payload"
    return f"(({p}->>'user_id') ~ '^[0-9]+$' AND ({p}->>'user_id')::bigint = ANY(%(user_ids)s))"


def filters_of(alias: str = "") -> str:
    col = f"{alias}.prompt_hash" if alias else "prompt_hash"
    return f"{col} IN (SELECT prompt_hash FROM user_filters WHERE user_id = ANY(%(user_ids)s))"
