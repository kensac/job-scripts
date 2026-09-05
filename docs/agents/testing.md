# Testing

Write tests. Prefer integration tests against a real database over mocks.

## A test must be able to fail

**Verify every new test fails against the code before your change.** A test
that passes both ways constrains nothing.

Failure modes seen repeatedly here:

- **Tautology**: the test joins on the same condition the code checks, so it
  cannot disagree.
- **Conditional vacuity**: the test asserts only on rows that already contain
  the field it checks. If the code stops emitting the field, zero rows match
  and it passes.
- **Wrong subject**: the test exercises a different route than the one whose
  behaviour it is named for.
- **Self-confirming fixture**: the test asserts against a recorder it
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

Prefer real shapes over invented ones. Real data carries the awkward cases.
Nulls where you expected values, strings where you expected numbers, absent
fields whose absence is the meaning. Those are where the bugs are.

## Three populations, and how a test picks one

Every test here is in exactly one of these. The marker says which. There is no
marker filter, so `pytest tests` runs all three.

**Unmarked.** A truncated database the test fills itself. Fast, hermetic, and
where almost everything belongs.

**`@pytest.mark.corpus`.** The generated corpus in `tests/corpus.py`. It is a
full catalog whose every value is drawn from `tests/production_profile.json`,
which `scripts/measure_profile.py` measures off production. Use it when the
subject is code running over realistic shape and volume: a query plan, a
predicate over a whole catalog, an aggregate across several users. It runs on
every pull request, because generated data needs no credential.

**`@pytest.mark.integration`.** A synced copy of real production. Skipped when
the database does not hold one. Use it only when the subject is something a
live writer produced and a generator cannot: what the comp extractor wrote,
what users typed, whether the reaper is still requeueing, hashes stored by an
older version of the code.

**Do not build the corpus to satisfy an assertion.** A corpus that reproduces
an invariant makes every test of that invariant a tautology, which is the
first failure mode listed above. When a test needs an invariant the generator
does not produce, that is the signal it belongs on real data, and it must say
so in its own docstring. `tests/test_prod_shapes.py` is the worked example.

## Keeping the corpus a measurement

The profile is the whole safety argument, and a measurement taken once is an
assumption again within a month. `scripts/measure_profile.py --check` runs on
a schedule against production and fails when production holds a shape the
generator cannot produce: a new categorical value, a column that started
holding nulls, a range that moved, a table nobody added to the profile.

When it fails, re-measure with `make profile`, commit the result, then check
that the corpus and the tests reading it still cover what it now says. Do not
silence it by widening a range by hand. Every number in that file is supposed
to be a fact about production, and once one of them is typed rather than
measured, none of them can be trusted.

The profile never carries identifying values. A column's literal values are
recorded only when it is low-cardinality, short, absent from
`measure_profile.IDENTIFYING`, and free of anything that looks like an address
or a token. Everything else is reduced to lengths and character classes. The
file is committed, so that rule is load-bearing rather than a nicety.

The synced copy is still real data and now anonymises on the way out:
`sync_testdb.ANONYMISE` rewrites the mailbox, the email addresses and the
OAuth tokens inside the `INSERT ... SELECT`, so they never leave the server
they were already on.

## Test databases

The test database name and port derive from the checkout, so parallel work
cannot collide. A test database must be named `*_test`, `*_ci`, or `test_*`,
and anything destructive must refuse a name that does not match.

One test process per database, enforced rather than assumed. Two concurrent
runs against one database truncate each other's rows between tests, and the
failure surfaces in whichever test lost the race, so the reader debugs their
own change.

## Fixtures must state what they mean

**Check a column's default before letting a fixture rely on it.** A fixture
that leans on a default can create the opposite of what the test asserts and
pass for the wrong reason. State the value the test depends on, even when it
matches the default. Especially then, because the default can change under a
test that never mentioned it.

## Diagnosing a failure

Reproduce before fixing. An intermittent failure needs a deterministic
reproduction before it needs a fix, or you cannot know you fixed it.

**A long-lived local test database goes stale against a moving main.** A missing column added by someone else's migration presents as a burst of unexplained failures that look like your own change. Recreate the test database before investigating a sudden cluster of failures.

Check the environment before the code: which database the run used, whether
anything else was running against it, and whether the container still exists.
A vanished container and a container with a vanished port mapping present
identically to a suite as "the database stopped existing".
