"""Which Authentik groups are administrators: JOBTRACKER_ADMIN_GROUPS.

A leaf module, so the startup seeds (api.db, api.config_keys) can name the
admin groups without importing api.auth, which imports both. Two callers had
hard-coded 'infra-admins' and ignored this variable, so a deployment that
renamed the group got admin routes for one set and signup and alert mail for
another.
"""

from __future__ import annotations

from core.env import env_list

ADMIN_GROUPS = frozenset(env_list("JOBTRACKER_ADMIN_GROUPS", "infra-admins"))
