# Generated files

Some files in this repository are produced by tooling rather than written. Two
of them are enforced in CI, and they behave differently: one is **checked**, the
other is **rewritten and pushed back to your branch.**

## `openapi.json`: regenerated and auto-committed

CI runs the schema export and compares. If the committed file is stale, CI
**commits the regenerated file to your branch and pushes it.**

What that means for you:

- **Regenerate it yourself whenever you change routes**, request models,
  response models, or framework versions that generate their schemas, and
  commit it with the change. `make schema` does it. A PR
  that changes an endpoint and not this file is incomplete, even though CI will
  paper over it.
- **After CI has run, your branch may be ahead of your local copy.** Pull before
  you push again, or your next push is rejected and you will reach for a force
  push you do not need.
- **A commit you did not write will appear in your branch.** It is expected.

**A run that pushes a fix fails and judges nothing.** It started from the
commit before the fix, so the pushed commit needs a run of its own. See
deployment.md.

**Only `gate` checks out the branch head**, because it has to push back to a
real branch, and it only lints and formats. Every other job checks out the
merge result (`github.sha` on a pull request), so anything that breaks only in
combination with main, such as two migration heads, fails on the pull request.

**Every operation declares what it returns.** `tests/test_openapi_current.py`
fails on a route without a response model (the resume PDF is the one named
exception), on a mangled schema name such as
`api__routers__source_admin__Source` (two routers naming two models the same
thing), and on a `Decimal` field. Declaring a shape must not change the
payload:

- Declare money as `float`. A `Decimal` field is served as a JSON string,
  where the bare dict served a number. A `numeric` sum of a token count
  declares `int`.
- A dict omits a key it has no value for; a model emits it as null. Where the
  old payload omitted keys, set `response_model_exclude_none=True` on the
  route and say so beside the model.
- A datetime serialises with `Z` rather than `+00:00`. Same instant.
- Define a response model above the route that returns it. Under `from
  __future__ import annotations` a later definition raises `PydanticUserError:
  ... is not fully defined` at import.

Running `npx openapi-typescript openapi.json` and reading the output is the
check that a generated client is usable; the tests can pass on a schema that
is self-consistent and wrong.

## The migration drift check: checked, never committed

`make migrations-check` applies every migration to the test database and runs
`alembic check`, which fails when autogenerate would emit any operation. A
failure means the ORM models and the migrations disagree. CI and `make check`
both run that target; it writes no file.

The usual cause is a column defined one way in the model and another in the
migration: a server default without a matching `nullable=False` is the common
one. Fix the mismatch and write a real migration; do not rename the throwaway.

## The rule

**Never hand-edit a generated file.** Change the source and regenerate. A
hand-edit survives until the next regeneration and then vanishes without
explanation, and in the meantime the file disagrees with the thing it is
supposed to describe.
