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
that the admin UI does not turn belongs in the host environment instead.

A value that names models or task purposes validates them against the
registries that define them (`core.providers.MODELS`, `core.shapes.SHAPES`),
as `verdict_model_equivalence` and `model_fallback_chains` do, so a typo is
refused on save instead of silently matching nothing.
