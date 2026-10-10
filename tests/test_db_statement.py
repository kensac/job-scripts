"""db's one-statement helpers run in autocommit, so a far host pays one round
trip a statement instead of three (BEGIN, the statement, COMMIT)."""

from __future__ import annotations

from api import db
from core import pool


def test_a_lone_statement_runs_outside_a_transaction_block():
    # now() is the transaction's start; statement_timestamp() is this
    # statement's. They differ only when a BEGIN came earlier.
    row = db.query_one("SELECT now() = statement_timestamp() AS fresh, 1 AS one")
    assert row == {"fresh": True, "one": 1}


def test_the_pooled_connection_goes_back_out_of_autocommit():
    db.query_one("SELECT 1")
    with pool.pool.connection() as conn:
        assert conn.autocommit is False


def test_a_statement_inside_a_transaction_joins_it():
    with pool.transaction():
        first = db.query_one("SELECT now() AS t")
        second = db.query_one("SELECT now() AS t")
    assert first == second
