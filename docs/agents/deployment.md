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

## Rolling order

The application runs `alembic upgrade head` at startup, so whichever member
of the fleet starts first on a new commit migrates production for all of
them. Containers first, then the laptop, is the order that keeps an old
worker from meeting a schema it does not know: an additive migration is
harmless in either direction, a column drop is not.

**That ordering assumes the laptop is on the same commit as the containers,
and nothing enforces it.** The laptop worker is a bare process on a source
checkout: no pin, no supervisor, no CD, no metrics. If it is updated by
`git pull` onto main it can run a migration the fleet has not rolled yet,
which is exactly what happened on 2026-09-04 (harmless only because the
migration created a table nothing older read). Update it with
`git checkout <the fleet's commit>`, never with `git pull`, so it matches
the container digest by construction. And ask for one roll per batch of
merges rather than one per merge: each laptop restart is manual.

## After a roll

Report the running digest per container, compared against the pin. A playbook
that reports "no changes" is not evidence that nothing happened, and a green
run is not evidence that the right image is live.

## Error tracking

Every record names its host from `JOBTRACKER_HOST_NAME`, else `JOBTRACKER_WORKER_NAME`, else the hostname, which inside a container is the container id and unreadable on a log page. Worker name and host name are different facts: a box running two workers (hetzner runs `hetzner` and `hetzner-2`) has one host and two worker names, and the fallback would stamp the second worker's records with a host that does not exist. So every container on such a box carries `JOBTRACKER_HOST_NAME=<fleet host>` explicitly; a host running one worker whose name is the host needs nothing. Every api and worker container carries `POSTHOG_API_KEY` and `POSTHOG_HOST` (rendered from Infisical by the config repos; the key is PostHog's public ingest key for the project the frontend already reports to). Without the key the telemetry layer is a no-op, so a container missing it runs and merely reports nothing. `JOBTRACKER_REVISION` is baked into the image from the build arg `GIT_SHA` by the GHCR workflow; nothing sets it at deploy time, and a local build without the arg reads `unknown`.

`POSTHOG_HOST` is PostHog's direct ingest host (https://us.i.posthog.com); the service does not go through the browser proxy, which exists to dodge ad blockers a server never meets and whose downtime would otherwise take the service's telemetry with it. Two volume knobs, read once at startup: `POSTHOG_TRACE_SAMPLE` (fraction of traces kept, default 1.0) and `POSTHOG_LOG_LEVEL` (lowest log level shipped, default INFO). Errors and events are never sampled. Each container's startup log carries one line naming the service, instance, host, release and both knobs, or DISABLED when the key is unset. Whether it is shipping is `jobtracker_telemetry_exports_total{kind,result}` on /metrics: records attempted by kind, ok or failed. Whether it is LANDING is a different question that no metric answers: PostHog's OTLP endpoint returns 200 to any token, a wrong one included, and to an empty body, so a fleet shipping with a stale project token reads as perfectly healthy. On 2026-09-05 it had for a day. After any change to `POSTHOG_API_KEY` the proof is a `jobtracker-api` span on the Tracing page within a minute of a request; the counter cannot be. The token is the project's ingest token (`phc_`), the same one the frontend bundle carries, and when the two disagree the frontend's deployed value is the current one.
