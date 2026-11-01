"""Comma lists, read one way: environment variables and provider fields.

It lives in core because core reads such lists too and cannot import api.
Query parameters have their own parser, `api.params.csv`.
"""

from __future__ import annotations

import os


def env_list(name: str, default: str = "") -> list[str]:
    """`A, B,,C` is ["A", "B", "C"]. Unset uses `default`; set to empty is the
    empty list, so a variable can switch a default list off."""
    return comma_list(os.environ.get(name, default))


def comma_list(value: str) -> list[str]:
    """`A, B,,C` is ["A", "B", "C"]."""
    return [v.strip() for v in value.split(",") if v.strip()]
