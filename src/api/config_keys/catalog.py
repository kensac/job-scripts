"""Persisted catalog settings and their admin metadata."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, NonNegativeInt, PositiveInt

from api.config_types import ConfigKey
from core.shapes import (
    CLASSIFY_LOCATIONS_PER_CYCLE,
    EXTRACT_COMP_PER_CYCLE,
    EXTRACT_REQUIREMENTS_PER_CYCLE,
    JOB_PROFILE_TASK,
    LOCATIONS_TASK,
)

CONFIG_KEYS: dict[str, ConfigKey] = {
    "compensation_demand_gate_enabled": ConfigKey(
        section="Catalog",
        default=False,
        value_type=bool,
        help="Extract compensation only after a posting passes an enabled personal filter, "
        "appears on a published managed board, or is personally tracked or uploaded. "
        "Existing verification and content freshness checks still apply. "
        "Already submitted batches finish normally.",
    ),
    # How stale a listing's last_seen_at may get before a pull that still
    # lists it rewrites the row only to move it. It is also how far behind its
    # source's newest row a listing may fall and still count as listed now
    # (core.catalog.LISTED_NOW). Refreshed every pull instead, the
    # timestamp alone rewrote 1.17M rows a day, 99.2% of them otherwise
    # unchanged (pg_stat_statements, 2026-10-03).
    "listings_seen_refresh_hours": ConfigKey(
        section="Catalog",
        default=24,
        value_type=PositiveInt,
        help="Hours a listed posting's last-seen time may lag before a pull rewrites the row "
        "just to update it. A posting further than this behind its board's most recently "
        "seen posting counts as no longer listed.",
    ),
    "source_title_patterns_enabled": ConfigKey(
        section="Catalog",
        default=True,
        value_type=bool,
        help="Whether source title patterns restrict which fetched postings enter the catalog. "
        "When disabled, every fetched posting is admitted while listings.kept still records "
        "whether the stored pattern matched, so the bypass remains measurable and reversible.",
    ),
    "verification_reachability_gate_enabled": ConfigKey(
        section="Catalog",
        default=False,
        value_type=bool,
        help="Restrict new posting verification to jobs that can reach an entitled personal "
        "filter or a published managed board after free date, location and enforced title gates. "
        "Tracked and directly imported jobs remain eligible.",
    ),
    "classify_locations_max_output_tokens": ConfigKey(
        section="Catalog",
        default=LOCATIONS_TASK.max_output_tokens,
        value_type=Annotated[int, Field(ge=256, le=64000)],
        help="Output-token ceiling for new location classification batches. Multi-city strings "
        "need room for every place. Incomplete answers remain rejected; existing paid batches "
        "keep their original limit. Actual spend uses tokens emitted, not this ceiling.",
    ),
    # Distinct location strings classified per hourly cycle. The backlog is
    # 8,735 strings; set low for a first look at GET /admin/locations, then
    # raised to clear it in one cycle.
    "classify_locations_per_cycle": ConfigKey(
        section="Catalog",
        default=CLASSIFY_LOCATIONS_PER_CYCLE,
        value_type=PositiveInt,
        help="Distinct location strings the hourly classification cycle sends to the model.",
    ),
    "job_profile_collection_enabled": ConfigKey(
        section="Catalog",
        default=True,
        value_type=bool,
        help="Collect new shared job profiles. Turning this off pauses scheduled, manual, "
        "and queued work before submission. Existing profiles stay available and submitted "
        "batches still finish. Turning it back on resumes collection of eligible postings.",
    ),
    # Whether the hourly requirements extraction runs. Off since 2026-09-07:
    # its one consumer is the market table, and the deployed arm was measured
    # at a third of the reference's skills with seniority mostly blank. Turn
    # on from the admin config page after choosing a model worth paying for.
    "requirements_extraction_enabled": ConfigKey(
        section="Catalog",
        default=False,
        value_type=bool,
        help="Whether the hourly requirements extraction runs. Off since 2026-09-07: its one "
        "consumer is the market table and the deployed arm measured poorly; pick an arm "
        "with an experiment before turning it on.",
    ),
    # Each per-cycle size below was an environment variable or a literal
    # until 2026-10-04. None was set on any host in homelab-config, so every
    # default is the value production ran. A size derived in core.shapes
    # keeps its derivation there, beside the number.
    "comp_extract_per_cycle": ConfigKey(
        section="Catalog",
        default=EXTRACT_COMP_PER_CYCLE,
        value_type=PositiveInt,
        help="Postings the hourly compensation extraction sends to the model per cycle. "
        "The fleet budget prices a cycle at this many.",
    ),
    "requirements_extract_per_cycle": ConfigKey(
        section="Catalog",
        default=EXTRACT_REQUIREMENTS_PER_CYCLE,
        value_type=PositiveInt,
        help="Postings the hourly requirements extraction sends to the model per cycle, "
        "when requirements_extraction_enabled is on. The fleet budget prices a cycle at "
        "this many.",
    ),
    "job_profiles_per_cycle": ConfigKey(
        section="Catalog",
        default=JOB_PROFILE_TASK.per_cycle,
        value_type=PositiveInt,
        help="Postings the shadow job-profile classification sends to the model per cycle. "
        "The fleet budget prices a cycle at this many.",
    ),
    # The newest first, so a backlog cannot starve the day's postings: on
    # 2026-09-15 214,306 active postings held no closed verdict and 3,440 of
    # them were posted within three days, so one cycle covers every fresh
    # posting and the remainder drains behind it.
    "verify_new_per_cycle": ConfigKey(
        section="Catalog",
        default=4000,
        value_type=PositiveInt,
        help="Postings without a closed or clearance verdict that the hourly verification "
        "sends to the model per cycle, newest first.",
    ),
    "reverify_days": ConfigKey(
        section="Catalog",
        default=7,
        value_type=PositiveInt,
        help="Days a posting on someone's board keeps its closed verdict before the hourly "
        "re-verification fetches the page and asks again.",
    ),
    # An emergency brake, not a size: the re-check population is whatever
    # went stale, and the shadow report compares that whole population and
    # shows this cap beside it.
    "reverify_per_cycle": ConfigKey(
        section="Catalog",
        default=0,
        value_type=NonNegativeInt,
        help="Most postings one re-verification cycle re-checks. 0 means no limit.",
    ),
}
