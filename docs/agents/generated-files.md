# Generated files

Some files in this repository are produced by tooling rather than written. Two
of them are enforced in CI, and they behave differently: one is **checked**, the
other is **rewritten and pushed back to your branch.**

## `openapi.json`: regenerated and auto-committed

CI runs the schema export and compares. If the committed file is stale, CI
**commits the regenerated file to your branch and pushes it.**

What that means for you:

- **Regenerate it yourself whenever you change routes**, request models or
  response models, and commit it with the change. `make schema` does it. A PR
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

**The file pins requests, not responses.** Only a handful of operations declare
a response schema; the rest are empty objects. So it will catch a bad path,
method, or query parameter, and it will never catch a client and server
disagreeing about the shape of a response. Where you can declare a response
schema, do. It is the only place that class of drift becomes machine-visible.

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
