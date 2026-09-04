# Deployment

## Verify, never infer

**Never claim something is deployed because it merged.** Hosts follow a pinned
image digest, and main can be many commits ahead of the pin every host is
correctly running.

**Verify by comparing the running image's registry digest against the pin.**
Do not use a container's local image id: it is a different identifier that
coincides with the registry digest on some architectures and not others, and
reading it as a version produces confident wrong answers.

**Gate a roll on commit ancestry, not on a build timestamp.** Resolve the
digest to its source commit, then verify the commit you need is an ancestor of
it. A timestamp cannot tell you whether the commit behind an image was on a
working main — and an image built while main was broken is an outage, not
merely a stale one.

## Checking whether a route is live, without access

An **unauthenticated** request to the production API distinguishes a deployed
route from an absent one: an existing route returns 401 from its auth
dependency, an absent one returns 404. Include a known-fake path as a control.

This is safe because the authorisation check fails before any write. It is not
a licence to send an *authenticated* request — that provisions a user row, and
the rule against it stands.

**Do not use `git merge-base` against a commit from another repository.** A SHA
the repository does not contain exits with the same status as a commit that is
genuinely not an ancestor, so an unknown SHA fabricates a confident
"not deployed" answer. Deployment records from an infrastructure repository
refer to that repository's commits, not this one's.

## The pin

The pin is a snapshot, not a follow. Automation proposes pin updates, but its
registry lookups are cached, so a pin proposal can sit frozen on a stale digest
for a long time while the automation is demonstrably alive and running.

Do not wait on it. If a specific commit needs to reach a host, push the pin
bump and let the automation rebase onto it.

The deployed digest does not need to equal the newest build. It needs to
contain the commits you care about.

## Interrupting work

A deploy recreates containers, which kills in-flight work gracefully: the task
requeues without consuming a retry, and its identity survives.

**But a long task restarts from the beginning.** Back-to-back deploys can
prevent a long operation from ever completing. Before rolling, check what is
running and how long it has been running.

Work that holds progress in the database survives arbitrarily many restarts.
Work that holds progress in memory discards it every time. Prefer the former
for anything long.

## After a roll

Report the running digest per container, compared against the pin. A playbook
that reports "no changes" is not evidence that nothing happened, and a green
run is not evidence that the right image is live.
