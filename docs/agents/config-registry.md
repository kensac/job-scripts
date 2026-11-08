# Persisted setting registry

Every setting an administrator can change is an `app_config` row. Define its
key, default, type, help, choices, kind, and section together in the matching
`api/config_keys/` section module. Keep one entry per key. Do not add a
parallel default or list of keys in the API, worker, or frontend.

`api.config.CONFIG_KEYS` assembles the section modules for database seeding and
GET `/admin/config`. Its `ConfigKey` validation lives in `api.config_types`.
Keep that validation strict, including the distinct handling of host names,
board columns, and group policies. A key belongs to the section named by its
entry; section registries must not share a key. Keep entries in their
intended display order within each section.

Read the persisted value at the point of use. A host-specific deployment fact
that the admin UI does not turn belongs in the host environment instead. Which
task kinds a host claims is one: `JOBTRACKER_WORKER_KINDS` (allowlist) or
`JOBTRACKER_WORKER_EXCLUDE_KINDS` (denylist) in that host's compose
environment, read by `api.worker_lifecycle.claims`, never an `app_config` key.

The one write path is PUT `/admin/config/{key}` (`put_config` in
`api/routers/admin/config.py`). In one transaction it takes
`pg_advisory_xact_lock(hashtextextended('app_config:' || key, 0))`, records an
`app_config_changes` row with the old value and the acting admin, then upserts
`app_config`. A write from a script runs the same transaction; a bare upsert
leaves no history and can race a concurrent save.

A value that names models or task purposes validates them against the
registries that define them (`core.providers.MODELS`, `core.shapes.SHAPES`),
as `verdict_model_equivalence` and `model_fallback_chains` do, so a typo is
refused on save instead of silently matching nothing.
