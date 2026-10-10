"""Comma-list environment variables, read one way.

It lives in core because core reads such lists too and cannot import api.
Query parameters have their own parser, `api.params.csv`.
"""

from __future__ import annotations

import os


def env_list(name: str, default: str = "") -> list[str]:
    """`A, B,,C` is ["A", "B", "C"]. Unset uses `default`; set to empty is the
    empty list, so a variable can switch a default list off."""
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]
