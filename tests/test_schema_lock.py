"""A process waiting for a peer's migration must not stop that migration
from building an index CONCURRENTLY, which waits for every older
transaction and snapshot in the database to end."""

from __future__ import annotations

import threading

import psycopg

from api import db
from core.pool import pool


def test_a_process_waiting_for_the_schema_lock_does_not_block_a_concurrent_index_build():
    holder = psycopg.connect(str(pool.conninfo), autocommit=True)
    holder.execute("CREATE TABLE schema_lock_probe (x int)")
    holder.execute("SELECT pg_advisory_lock(%s)", (db._SCHEMA_LOCK_KEY,))
    waiter = threading.Thread(target=db.init_schema, daemon=True)
    waiter.start()
    try:
        # Long enough to see the waiter's first attempt; the build then has
        # to finish while the waiter is still waiting.
        threading.Event().wait(1.5)
        assert waiter.is_alive()
        holder.execute("SET statement_timeout = '10s'")
        # Raises (deadlock detected, or the timeout) if the waiter holds a
        # transaction or a snapshot while it waits.
        holder.execute("CREATE INDEX CONCURRENTLY schema_lock_probe_x ON schema_lock_probe (x)")
    finally:
        holder.execute("SELECT pg_advisory_unlock(%s)", (db._SCHEMA_LOCK_KEY,))
        waiter.join(timeout=60)
        holder.execute("DROP TABLE schema_lock_probe")
        holder.close()
    assert not waiter.is_alive()
