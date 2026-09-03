# Testing

Write tests. Prefer integration tests against a real database over mocks.

## A test must be able to fail

**Verify every new test fails against the code before your change.** A test
that passes both ways constrains nothing.

Failure modes seen repeatedly here:

- **Tautology** — the test joins on the same condition the code checks, so it
  cannot disagree.
- **Conditional vacuity** — the test asserts only on rows that already contain
  the field it checks. If the code stops emitting the field, zero rows match
  and it passes.
- **Wrong subject** — the test exercises a different route than the one whose
  behaviour it is named for.
- **Self-confirming fixture** — the test asserts against a recorder it
  installed, not against the behaviour.

A good pattern: supply input the code is expected to *reject*, so a broken
filter produces output and fails the test.

## Fixtures and mocks

**A fixture cannot falsify the assumption it was built from.** If you write
both the fixture and the code that consumes it, a test between them proves
only that you were consistent.

Verify fixture shapes against the source that produces them in production, not
against your expectation of it. When a contract is not machine-checkable,
shape drift is invisible and silent.

Prefer a disposable copy of real data over fabricated rows. Real data carries
the awkward cases — nulls where you expected values, strings where you
expected numbers, absent fields whose absence is the meaning — and those are
where the bugs are.

## Test databases

The test database name and port derive from the checkout, so parallel work
cannot collide. A test database must be named `*_test`, `*_ci`, or `test_*`,
and anything destructive must refuse a name that does not match.

One test process per database, enforced rather than assumed. Two concurrent
runs against one database truncate each other's rows between tests, and the
failure surfaces in whichever test lost the race — so the reader debugs their
own change.

## Diagnosing a failure

Reproduce before fixing. An intermittent failure needs a deterministic
reproduction before it needs a fix, or you cannot know you fixed it.

Check the environment before the code: which database the run used, whether
anything else was running against it, and whether the container still exists.
A vanished container and a container with a vanished port mapping present
identically to a suite as "the database stopped existing".
