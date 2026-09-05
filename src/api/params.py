"""Set-shaped list filters, one way everywhere.

`status=pending,running` is a list; a single value is the one-item case. An
endpoint that takes lists echoes `filters` with the values it applied, as
arrays, so the frontend can tell "lists accepted" (the key is present) from
"one value only" (an older build) without guessing, and can render the
active filter without duplicating the default.
"""

from __future__ import annotations


def csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v and v.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


def applied(**lists: list[str]) -> dict[str, list[str]]:
    """The `filters` echo: only the filters that narrowed anything."""
    return {key: values for key, values in lists.items() if values}
