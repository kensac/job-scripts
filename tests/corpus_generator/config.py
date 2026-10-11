from __future__ import annotations

import string
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().parent.parent / "production_profile.json"

# Jobs is the anchor: every other table is scaled by the ratio production has
# to it, so the corpus keeps production's proportions. 2000 clears the >1000
# floor the analytics assertions need with room to spare, and the whole build
# stays inside a few seconds.
TARGET_JOBS = 2000

# A floor, because scaling everything off jobs shrinks the small tables to
# nothing: user_filters is 10 rows in production, and 10 x 0.038 is zero
# filters, which is a corpus where no board can ever materialise. The floor is
# four rows per user, so every user has filters, sources and a history.
MIN_ROWS = 4 * 5

# More than one, which is the entire class of behaviour the copy can never
# cover: production has a single user row and a single distinct user_id in
# user_jobs, so every isolation and cross-user-leak assertion over the copy is
# vacuously true.
USERS = 5

# Fixed, so a failure is reproducible from the commit alone. A corpus that
# reshuffled per run would turn any shape-sensitive bug into a flake.
SEED = 294

# Joins the schema does not declare. The catalog's spine is jobs.url TEXT, not
# jobs.id - verdicts, requirements, skills and embeddings all key on the URL -
# so a generator that followed only foreign keys would emit four large tables
# that join to nothing, and every assertion over them would pass on zero rows.
# That is the "conditional vacuity" failure docs/agents/testing.md names.
LINKS = {
    ("ai_queries", "url"): ("jobs", "url"),
    ("page_fetches", "url"): ("jobs", "url"),
    ("job_skills", "url"): ("jobs", "url"),
    ("job_requirements", "url"): ("jobs", "url"),
    ("job_embeddings", "url"): ("jobs", "url"),
    ("job_comp", "url"): ("jobs", "url"),
    ("job_requirements", "content_row_id"): ("page_fetches", "id"),
    ("job_embeddings", "content_row_id"): ("page_fetches", "id"),
    ("job_comp", "content_row_id"): ("page_fetches", "id"),
    # Verdicts are keyed to a filter by the HASH of its prompt, not by its id.
    # Drawn from anywhere else, no custom verdict would ever match an enabled
    # filter and every user's board would be permanently empty.
    ("ai_queries", "prompt_hash"): ("user_filters", "prompt_hash"),
    ("ai_queries", "batch_id"): ("ai_batches", "provider_batch_id"),
    ("ai_batches", "task_id"): ("tasks", "id"),
    ("tasks", "parent_id"): ("tasks", "id"),
    ("user_sources", "source"): ("sources", "name"),
}

# Half of every parent draw comes from a small hot slice of that parent.
#
# Two things need it. Without it, 5 users spread over 2000 jobs share nothing,
# and "two people looking at the same posting" - the case the whole multi-user
# product turns on - never occurs in the corpus. And verdicts drawn uniformly
# over 2000 urls give each url 1.7 of them, so no job ever accumulates the
# closed + clearance + custom set a board row requires, and every board comes
# out empty. Production correlates both ways because rows are written by a
# pipeline that revisits the same posting; a marginal distribution cannot say
# so, and this is where that structure is put back.
HOT_SLICE = 0.05
HOT_SHARE = 0.5

# Filled by the app's own seed rather than by the profile. Deliberately just
# these two: nothing seeds `sources` any more, so the profile's sixteen real
# source names are the only thing that puts a catalog there at all. They used
# to come from configs.toml, which was never in the repository, so the seed
# wrote nothing on CI even before it was removed.
SEEDED = {"app_config", "group_budgets"}

# Never generated. alembic_version says which migration built the schema.
SKIP = {"alembic_version"}

_LETTERS = string.ascii_lowercase
_UPPER = string.ascii_uppercase
_DIGITS = string.digits
_PUNCT = "-_.,:/()&+"
