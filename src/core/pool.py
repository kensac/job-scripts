"""The one connection pool, and the instrumentation that has to precede it.

There were two. core/store.py opened one and api/db.py opened another, both
against the same DATABASE_URL, both min_size=1 max_size=10, and both are
imported by the API and by every worker: instrumenting ConnectionPool.__init__
while importing api.app showed two pools built with identical arguments. So a
process configured for ten connections could hold twenty, and core/catalog.py
reached for the private one by name (`from core.store import _pool as pool`)
because there was no shared one to ask for.

max_size is the sum of the two it replaces, not a new judgement about how many
connections this application needs. Nothing measures pool usage today, so
picking a smaller number here would be a capacity change smuggled into a
structural fix. JOBTRACKER_DB_POOL_MAX exists so it can come down once someone
has looked.
"""

from __future__ import annotations

import atexit
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import dotenv
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

dotenv.load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# The sum of the two pools this replaces. See the module docstring.
MAX_SIZE = int(os.environ.get("JOBTRACKER_DB_POOL_MAX", "20"))

# Instrumented BEFORE the pool opens its first connection: the instrumentor
# wraps connections as they are made, so a connection opened here at import
# and instrumented later in telemetry.init produced no spans for the queries
# it ran. Kanishk's trace of a 7 s board read showed the count query and then
# six seconds of nothing, which was the page query on that first connection.
# Without a tracer provider yet this is a no-op that becomes live once
# telemetry.init sets one.
#
# This lives beside the pool rather than in api/db.py, which is where it was.
# The ordering is the whole point of it, and with the pool created in one
# module and the instrumentation called in another, whichever module imported
# first decided whether the ordering held.
try:
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    PsycopgInstrumentor().instrument()
except ImportError:  # pragma: no cover - the exporter is a runtime dependency
    pass

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=MAX_SIZE,
    kwargs={"row_factory": dict_row},
    open=True,
)
atexit.register(pool.close)


_transaction_connection: ContextVar[Connection[dict[str, Any]] | None] = ContextVar(
    "transaction_connection", default=None
)


@contextmanager
def connection() -> Iterator[Connection[dict[str, Any]]]:
    active = _transaction_connection.get()
    if active is not None:
        yield active
    else:
        with pool.connection() as conn:
            yield conn


@contextmanager
def transaction() -> Iterator[None]:
    """Keep helper calls and nested board writes on one atomic connection."""
    with connection() as conn, conn.transaction():
        token = _transaction_connection.set(conn)
        try:
            yield
        finally:
            _transaction_connection.reset(token)
