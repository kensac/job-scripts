# Test performance

Measured on GitHub-hosted Ubuntu runners on 2026-09-09, revision
`3271201a9fce2757a093d8da2e199e8a17284f97`.
[Experiment and artifacts](https://github.com/kensac/job-scripts/actions/runs/34328307413).

## Measured comparison

Each parallel trial used three deterministic shards of tests without the
`corpus` marker and one corpus job, with an independent PostgreSQL database
per job. Both trials matched the serial run's 1,471 test IDs and outcomes
exactly: 1,457 passed and 14 skipped. Subtests remain inside their parent case.

| Trial | Serial pytest | Parallel pytest critical path | Serial job | Parallel job span | Parallel runner total |
|---|---:|---:|---:|---:|---:|
| 1 | 171.52s | 96.47s | 201s | 135s | 350s |
| 2 | 185.86s | 57.90s | 214s | 90s | 307s |

Job times include provisioning, dependency installation and artifact upload.
Parallel span is the earliest parallel job start through the last job finish;
it is not the entire CI workflow, which also performs static checks and a
final coverage check. Queue delays before jobs start are excluded. Two trials
support adopting this split, but do not establish a stable speedup: one shard
was considerably slower in trial 1, and the cause was not determined.
Parallel execution consumes more total runner time.

## Where time goes

In ordinary, unprofiled serial execution, setup phases consumed 132.81s and
145.02s; test call phases consumed 31.18s and 33.17s. Teardown was about 0.32s.
Four corpus setup phases each took about six seconds. Grouping corpus tests
into their own job also avoids rebuilding it between ordinary tests.

A separate cProfile run counted four corpus builds and 1,433 `_reseed` calls.
The current `_reseed` implementation inserts two group defaults and 24
application defaults individually, so those calls account for 37,258 default
insert executions. Profiled cumulative times overlap and instrumentation
substantially increases runtime; they must not be used as ordinary wall times.

The worker entrypoint test's call took about 8.02 seconds in both serial runs.
Its source waits a fixed eight seconds to detect process fallthrough.

## Adopted design and next experiments

Normal CI shares its lane implementation with the manual benchmark. A full
collection manifest and the lane reports must identify the same commit and
account for every case exactly once before the required `check` passes.
Each lane retains the existing database isolation and cleanup fixtures.
Lint, formatting, type checks, compilation, migration checks and extension
tests remain required. Mechanical lint and formatting corrections still run
before the tests.

Prioritize subsequent work in this order:

1. Batch default reseeding and measure setup time again. Preserve fresh state
   per test, including defaults and sequences; transaction rollback is not a
   replacement for tests that commit or use separate connections.
2. Introduce an explicit database-free test population after auditing actual
   imports and fixture dependencies. A missing `db` fixture argument alone
   cannot establish that a test does not access the database.
3. Replace the worker test's fixed grace period with observable worker-loop
   progress and a bounded failure timeout. Process liveness alone does not
   prove it reached the loop.
4. Revisit shard balancing only after repeated timing reports show a persistent
   imbalance. Keep deterministic assignment and exact coverage verification.

These follow-up changes are recommendations, not measured implementations.

## Reproduce

Dispatch the `Test performance` workflow on a selected revision. It runs two
serial and parallel trials plus one separate call profile. Download its
`timings-*` artifacts, preserving their directories, then run:

```sh
python tools/test_performance.py measurements --repetitions=2 --shards=3
```

The comparison rejects missing, duplicated or changed test outcomes and
reports from different revisions. Compare job timestamps as well as pytest
phase times before changing job counts. Normal CI also uploads these phase
reports, so later changes can be evaluated without adding a profiler to the
regular test path.
