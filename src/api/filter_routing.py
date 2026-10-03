"""Best-effort shadow observations, never verdicts or projection inputs."""

import logging
from typing import Any

from api import db
from core.filter_policy import RoutingPolicy, propose
from core.job_profile import CLASSIFIER_VERSION, JOB_PROFILE_MODEL, JobProfileAnswer

logger = logging.getLogger(__name__)


def load_policy() -> RoutingPolicy | None:
    try:
        with db.transaction():
            policy = RoutingPolicy.model_validate(db.get_config("filter_routing_policy"))
        if policy.profile_mode == policy.title_mode == policy.ambiguity_mode == "off":
            return None
        return policy
    except Exception:
        logger.exception("Filter routing configuration unavailable; retaining detailed review")
        return None


def observations(
    policy: RoutingPolicy | None,
    prompt_hash: str,
    jobs: list[dict[str, Any]],
    contents: dict[str, str],
    model: str | None = None,
) -> dict[str, dict[str, Any]]:
    if policy is None:
        return {}
    try:
        profiles = {}
        if policy.profile_mode == "shadow" and prompt_hash in policy.profiles:
            # Compare the actual immutable review input, not today's content pointer.
            # A different generation abstains even when its URL is unchanged.
            # The profile's title input is not independently persisted, so these
            # observations remain shadow-only even when cached text agrees.
            with db.transaction():
                previous = db.query_one("SELECT current_setting('statement_timeout') AS value")
                assert previous is not None
                db.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{policy.observation_timeout_ms}ms",),
                )
                rows = db.query(
                    "SELECT DISTINCT ON (p.url) p.* FROM job_profiles p "
                    "JOIN ai_queries q ON q.id = p.content_row_id "
                    "JOIN unnest(%s::text[], %s::text[]) AS inputs(url, content) "
                    "ON inputs.url = p.url AND inputs.content = q.input_content "
                    "WHERE p.classifier_version = %s AND p.model = %s "
                    "ORDER BY p.url, p.id DESC",
                    (
                        list(contents),
                        list(contents.values()),
                        CLASSIFIER_VERSION,
                        JOB_PROFILE_MODEL,
                    ),
                )
                db.execute("SELECT set_config('statement_timeout', %s, true)", (previous["value"],))
            for row in rows:
                profiles[row["url"]] = row
        result = {}
        for job in jobs:
            if job["url"] not in contents:
                continue
            row = profiles.get(job["url"])
            proposal = propose(
                policy,
                prompt_hash,
                JobProfileAnswer.model_validate(row) if row else None,
                profile_id=row["id"] if row else None,
                title=job.get("title") or "",
                model=model,
            )
            result[job["url"]] = proposal.model_dump(mode="json")
        return result
    except Exception:
        logger.exception("Filter routing observation failed; retaining detailed review")
        return {}
