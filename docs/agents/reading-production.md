# Reading production from outside it

**Anything that reads the production database shares production's blast
radius**, including development tooling. A tool that only reads is not
therefore safe.

## A long read blocks schema changes

A bulk read holds a shared lock for its whole duration. A queued `ALTER TABLE`
waits behind it, and everything after that queues behind the ALTER.

The application runs migrations at startup, so a read that outlives a deploy
prevents the application from starting at all. Containers sit at "waiting for
application startup" while every health check, image digest and container
status reports normal.

## Build so it cannot happen

The property to build for is that such a tool **cannot** hold a lock long
enough to matter, not that someone remembers when to run it:

- **Chunk the read** so no cursor outlives a deploy. One transaction per range
  means a queued DDL waits one chunk instead of one table. This also makes the
  copy resumable, which a large transfer needs regardless.
- **Set `idle_in_transaction_session_timeout` on the reading role** as the
  backstop. A tool that dies at the client end can otherwise leave a cursor
  pinned indefinitely. The danger window is not the run's duration; it is
  unbounded until someone kills the connection.
- Set both as role-level defaults so a future caller cannot forget them.
- **Better than any timeout: do not keep the connection alive.** A process
  that exits when its work finishes cannot leak a lock, because the state is
  gone rather than bounded. A long-lived container holding a pool is what
  turns a finished job into an open transaction.

The exposure window is **not the duration of the run.** A connection can
outlive the job that opened it, so "is a copy running right now" returns no
while a lock is still held. Ask what connections exist, not what jobs are
running.

Two levers look right and are not. `statement_timeout` caps how long the read
runs, but the DDL still queues for that whole period. And any timeout long
enough to copy a large table is long enough to stall a deploy. `lock_timeout`
governs locks a session **waits for**, not ones it **holds**.

## A foreign-data-wrapper session is opaque from the source side

It shows as `FETCH n FROM cN` with no table name, so grepping the source for
what you think it is reading finds nothing and proves nothing.

Identify it by client address and application name. But note that a host can
present **more than one public IP** depending on egress path, and the same
resolver can return different answers from different processes on that host.

One reading does not identify a machine. Check several, and prefer a causal
test (stop the suspected process, see if the session goes) over an address
match.
