from __future__ import annotations

import argparse
import os
import re
import sys

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict


def require_disposable_name(name: str, *, allow_dev: bool = False) -> str:
    # Restrict names to identifiers: callers also use them in database creation,
    # and libpq can interpret a dbname containing '=' or a URI as another DSN.
    suffixes = ("_test", "_ci", "_dev") if allow_dev else ("_test", "_ci")
    prefixes = ("test_", "dev_") if allow_dev else ("test_",)
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) or not (
        name.endswith(suffixes) or name.startswith(prefixes)
    ):
        allowed = "*_test, *_ci, or test_*"
        if allow_dev:
            allowed += ", or *_dev or dev_*"
        raise RuntimeError(f"refusing database: name it {allowed}")
    return name


def require_disposable_dsn(dsn: str, *, allow_dev: bool = False) -> str:
    # Use the driver's parser: URL paths alone miss query-string dbname
    # overrides. Do not expose parser errors, which can contain credentials.
    try:
        name = conninfo_to_dict(dsn).get("dbname", "")
    except ProgrammingError:
        raise RuntimeError("refusing database: invalid PostgreSQL connection string") from None
    return require_disposable_name(str(name or ""), allow_dev=allow_dev)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a disposable database without connecting"
    )
    parser.add_argument(
        "--env", required=True, help="environment variable holding the connection string"
    )
    parser.add_argument("--allow-dev", action="store_true")
    args = parser.parse_args()
    try:
        require_disposable_dsn(os.environ.get(args.env, ""), allow_dev=args.allow_dev)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
