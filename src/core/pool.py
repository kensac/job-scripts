"""Shared database pool and transaction-aware connections.

Use connection() for reads and writes that must join an existing transaction.
Use transaction() around atomic domain writes, usage and receipt acknowledgement.
Keep provider calls outside database transactions. Pool capacity is configurable
through JOBTRACKER_DB_POOL_MAX; changing it requires a separate measurement.
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

# Preserve the combined capacity of the original API and storage pools.
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
