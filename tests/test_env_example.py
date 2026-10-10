"""`.env.example` lists exactly the environment variables the code reads.

A template was deleted once (#505) because it drifted from the code and named
variables nothing read. This keeps the second copy honest both ways: a
variable the code starts reading fails here until it is listed, and one the
code stops reading fails here until it is removed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

READS = re.compile(
    r"""(?:environ(?:\.get)?\s*[\[(]|getenv\(|env_list\(|_required_env\(|api_key_env=)"""
    r"""\s*["']([A-Z][A-Z0-9_]*)["']"""
)
# Set by the code for its own children, or the operating system's, not config.
NOT_CONFIG = {"PGOPTIONS", "USER"}
# Read where the scan does not look: the Makefile and conftest read the local
# databases, and payload storage builds its names from a prefix.
READ_ELSEWHERE = {
    "TEST_DATABASE_URL",
    "JOBTRACKER_DEV_DATABASE_URL",
    *(
        f"JOBTRACKER_S3_{n}"
        for n in ("ENDPOINT", "REGION", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
    ),
}


def _read() -> set[str]:
    read: set[str] = set()
    for folder in ("src", "tools", "scripts", "alembic"):
        for path in (ROOT / folder).rglob("*.py"):
            read |= set(READS.findall(path.read_text()))
    assert "DATABASE_URL" in read  # the scan itself works
    return read - NOT_CONFIG


def _listed() -> set[str]:
    text = (ROOT / ".env.example").read_text()
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))


def test_every_variable_the_code_reads_is_listed():
    assert _read() - _listed() == set()


def test_every_listed_variable_is_read():
    assert _listed() - _read() - READ_ELSEWHERE == set()
