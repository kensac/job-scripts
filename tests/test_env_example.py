"""`.env.example` lists every environment variable the code reads.

A template was deleted once (#505) because it drifted from the code and named
variables nothing read. This keeps the second copy honest in one direction:
a variable the code starts reading fails here until it is listed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

READS = re.compile(
    r"""(?:environ(?:\.get)?\s*[\[(]|getenv\(|_env_list\(|_required_env\(|api_key_env=)"""
    r"""\s*["']([A-Z][A-Z0-9_]*)["']"""
)
# Set by the code for its own children, or the operating system's, not config.
NOT_CONFIG = {"PGOPTIONS", "USER"}


def test_every_variable_the_code_reads_is_listed():
    read: set[str] = set()
    for folder in ("src", "tools", "scripts", "alembic"):
        for path in (ROOT / folder).rglob("*.py"):
            read |= set(READS.findall(path.read_text()))
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.M))
    assert "DATABASE_URL" in read  # the scan itself works
    assert read - NOT_CONFIG - listed == set()
