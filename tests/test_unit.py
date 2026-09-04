from __future__ import annotations

import hashlib
import re

from api import ai, db, fetching, ssrf, worker
from api import criteria as crit
from api.tasks import runtime as tasks_runtime
from core import filters, pricing

# ---------------------------------------------------------------------------
# api.criteria
# ---------------------------------------------------------------------------


def test_criteria_params_collapses_unset():
    params = crit.params(None)
    assert params["crit_date"] is None
    assert params["crit_has_excl"] is False
    assert params["crit_excl"] == []


def test_criteria_params_collapses_new_criteria_unset():
    params = crit.params(None)
    assert params["crit_has_incl"] is False
    assert params["crit_incl"] == []
    assert params["crit_comp_min"] is None


def test_criteria_params_includes_lowercased_and_escaped():
    params = crit.params({"criteria": {"included_locations": ["New York (NY)", "  Remote  "]}})
    assert params["crit_has_incl"] is True
    assert params["crit_incl"] == [re.escape("new york (ny)"), re.escape("remote")]


def test_criteria_params_excludes_lowercased_and_escaped():
    params = crit.params({"criteria": {"excluded_locations": ["New York (NY)", "  UK  "]}})
    assert params["crit_has_excl"] is True
    assert params["crit_excl"] == [re.escape("new york (ny)"), re.escape("uk")]


def test_criteria_word_boundary_matching_through_postgres():
    params = crit.params({"criteria": {"excluded_locations": ["UK"]}})
    pattern = params["crit_excl"][0]

    def matches(location: str) -> bool:
        row = db.query_one(
            "SELECT lower(%(loc)s) ~ ('\\m' || %(pattern)s || '\\M') AS matched",
            {"loc": location, "pattern": pattern},
        )
        return bool(row["matched"])

    assert matches("London, UK") is True
    assert matches("Newcastle upon Tyne, UK") is True
    assert matches("Tukwila, WA") is False


def test_criteria_word_boundary_matching_canada():
    params = crit.params({"criteria": {"excluded_locations": ["Canada"]}})
    pattern = params["crit_excl"][0]

    def matches(location: str) -> bool:
        row = db.query_one(
            "SELECT lower(%(loc)s) ~ ('\\m' || %(pattern)s || '\\M') AS matched",
            {"loc": location, "pattern": pattern},
        )
        return bool(row["matched"])

    assert matches("Toronto, Canada") is True
    assert matches("Vancouver, BC") is False


# ---------------------------------------------------------------------------
# api.fetching.looks_blocked
# ---------------------------------------------------------------------------


def test_looks_blocked_none_content():
    assert fetching.looks_blocked(None) is False


def test_looks_blocked_short_page():
    assert fetching.looks_blocked("short") is True


def test_looks_blocked_short_page_with_markers():
    for marker in ("just a moment", "access denied", "service unavailable"):
        page = f"{marker} " * 30
        assert 300 < len(page) < 6000
        assert fetching.looks_blocked(page) is True


def test_looks_blocked_long_real_posting_with_cloudflare_word():
    page = "We use cloudflare for our website. " + ("Great engineering culture. " * 300)
    assert len(page) > 6000
    assert fetching.looks_blocked(page) is False


# ---------------------------------------------------------------------------
# api.tasks_runtime.AdaptiveLimiter
# ---------------------------------------------------------------------------


def test_adaptive_limiter_starts_at_min_when_max_is_low():
    limiter = tasks_runtime.AdaptiveLimiter(min_c=1, max_c=1)
    assert limiter.limit == 1


def test_adaptive_limiter_grows_on_sustained_throughput():
    limiter = tasks_runtime.AdaptiveLimiter(min_c=1, max_c=10, window=4)
    start = limiter.limit
    for _ in range(4):
        limiter.record()
    assert limiter.limit == start + 1


def test_adaptive_limiter_grows_across_two_windows(monkeypatch):
    limiter = tasks_runtime.AdaptiveLimiter(min_c=1, max_c=10, window=4)
    start = limiter.limit
    clock = iter([0, 8, 8, 15, 15])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    for _ in range(8):
        limiter.record()
    assert limiter.limit == start + 2


def test_adaptive_limiter_halves_on_rate_limit_signal():
    limiter = tasks_runtime.AdaptiveLimiter(min_c=1, max_c=10)
    limiter.limit = 8
    limiter.record(rate_limited=True)
    assert limiter.limit == 4


def test_adaptive_limiter_rate_limit_never_below_min():
    limiter = tasks_runtime.AdaptiveLimiter(min_c=2, max_c=10)
    limiter.limit = 2
    limiter.record(rate_limited=True)
    assert limiter.limit == 2


# ---------------------------------------------------------------------------
# api.ai
# ---------------------------------------------------------------------------


def test_validate_params_rejects_unknown():
    error = ai.validate_params("openai", {"bogus": 1})
    assert error is not None


def test_validate_params_accepts_valid_openai():
    assert ai.validate_params("openai", {"reasoning_effort": "medium"}) is None


def test_validate_params_accepts_valid_anthropic():
    assert ai.validate_params("anthropic", {"effort": "high"}) is None


def test_validate_params_rejects_invalid_effort():
    assert ai.validate_params("anthropic", {"effort": "bogus"}) is not None


def test_validate_params_temperature_follows_the_declared_capability():
    """Declared per provider now rather than a hardcoded provider name, so a
    provider that does accept temperature says so in its descriptor."""
    assert ai.validate_params("openai_compatible", {"temperature": 0.5}) is None
    assert ai.validate_params("openai", {"temperature": 0.5}) is not None


def test_provider_of_model():
    assert ai.provider_of_model("gpt-5-nano") == "openai"
    assert ai.provider_of_model("claude-sonnet-5") == "anthropic"
    assert ai.provider_of_model("no-such-model") is None


def test_prices_cover_every_catalog_model():
    for models in ai.MODEL_CATALOG.values():
        for entry in models:
            assert pricing.rates_for(entry["model"]) is not None, entry["model"]


# ---------------------------------------------------------------------------
# api.ssrf.validate_base_url
# ---------------------------------------------------------------------------


def test_validate_base_url_rejects_http():
    assert ssrf.validate_base_url("http://example.com") is not None


def test_validate_base_url_rejects_ip_literal():
    assert ssrf.validate_base_url("https://8.8.8.8/v1") is not None


def test_validate_base_url_rejects_single_label_host():
    assert ssrf.validate_base_url("https://localhost/v1") is not None


def test_validate_base_url_accepts_public_https():
    assert ssrf.validate_base_url("https://api.example.com/v1") is None


# ---------------------------------------------------------------------------
# core.filters.build_custom_instructions
# ---------------------------------------------------------------------------


def test_build_custom_instructions_stable_hash():
    first = filters.build_custom_instructions("must offer visa sponsorship", "filter")
    second = filters.build_custom_instructions("must offer visa sponsorship", "filter")
    assert first == second
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()


def test_canonicalising_redirects_are_not_treated_as_closures():
    """Real boards rewrite a posting URL without moving the posting, and
    reading that as a closure marked 74 live jobs dead in production.

    Both examples are the actual redirects those sites serve.
    """
    from api.fetching import redirected_away

    # jobs.apple.com appends a title slug to the id.
    assert (
        redirected_away(
            "https://jobs.apple.com/en-us/details/200681316",
            "https://jobs.apple.com/en-us/details/200681316/cellular-layer-1-control",
        )
        is False
    )
    # careers.amd.com rewrites the path prefix but keeps the id.
    assert (
        redirected_away(
            "https://careers.amd.com/jobs/90297?icims=1",
            "https://careers.amd.com/careers-home/jobs/90297?icims=1",
        )
        is False
    )
    # An ATS migration that keeps the id is still the same posting.
    assert redirected_away("https://a.test/jobs/90297", "https://b.test/jobs/90297") is False
    # Losing the id is what actually signals the posting is gone.
    assert redirected_away("https://a.test/jobs/90297", "https://a.test/jobs") is True
    assert redirected_away("https://a.test/jobs/90297", "https://a.test/careers") is True


def test_redirected_away_detects_board_index_bounce():
    from api.fetching import redirected_away

    job = "https://job-boards.greenhouse.io/hpiq/jobs/6173700004"
    # The real failure: expired posting bounces to the board index.
    assert redirected_away(job, "https://job-boards.greenhouse.io/hpiq?error=true") is True
    assert redirected_away(job, "https://example.com/careers") is True
    # Benign variations must not read as a redirect.
    assert redirected_away(job, job) is False
    assert redirected_away(job, job + "/") is False
    assert redirected_away(job, job + "?utm_source=x") is False
    assert redirected_away(job, None) is False


# ---------------------------------------------------------------------------
# Sort-key contract with the frontend
# ---------------------------------------------------------------------------


def test_sort_keys_the_frontend_depends_on_still_exist():
    """The admin UI maps its visible columns to these key names.

    It maps deliberately rather than rendering whatever the API says is
    sortable, which keeps the column layout intentional - but it means removing
    a key here degrades that column to default order SILENTLY, with no error
    anywhere. This test is the alarm, so the coupling does not rely on someone
    remembering to mention it.

    If you are removing a key on purpose: update this list and tell whoever
    owns app/job-scripts, because a column needs to change with it.
    """
    from api.routers.admin import _JOBS_SORTABLE, _SORTABLE

    # /admin/jobs - the catalog table's columns.
    assert {"company", "failed", "total_tokens", "last_seen"} <= set(_JOBS_SORTABLE)
    # /admin/queries - the responses table's columns.
    assert {"created_at", "total_tokens", "duration_ms"} <= _SORTABLE


def test_verdicts_take_their_timestamp_from_the_database():
    """`ai_queries.created_at` must come from Postgres, not from Python.

    verify.py compares it against `ai_batches.submitted_at`, which Postgres
    writes with now(). That inequality is the whole of the rule that a parked
    reverify may not overturn a closure newer than its evidence - and three
    worker hosts mean three clocks against one database. A host running
    slightly fast makes its verdicts look newer than batches submitted after
    them, and a reverify that should record is discarded as stale, silently.

    Asserting on the insert column list rather than on behaviour because the
    two clocks agree closely enough in a test that a behavioural check would
    pass either way - which is exactly why this shipped.
    """
    from core.store import _INSERT_COLUMNS

    assert "created_at" not in _INSERT_COLUMNS


def test_requirements_declares_a_shape_the_model_can_actually_serve():
    """Every batched extraction goes through the router so its model is checked
    against declared capability rather than assumed. Requirements was the last
    one naming a model straight into the batch call."""
    from api.tasks.requirements import REQUIREMENTS_MODEL, REQUIREMENTS_TASK
    from core import providers
    from core.providers import StructuredOutput
    from core.routing import resolve

    assert REQUIREMENTS_TASK.candidates == (REQUIREMENTS_MODEL,)
    assert REQUIREMENTS_TASK.structured is StructuredOutput.JSON_SCHEMA
    assert REQUIREMENTS_TASK.batched is True

    assert resolve(REQUIREMENTS_TASK).model == REQUIREMENTS_MODEL
    # The effort is DERIVED, not pinned. luna rejects "minimal" and mini
    # rejects "none", so a literal here makes the model unswappable and a
    # batch fails whole on a 400 - #179. Assert it is legal for whichever
    # model the shape resolves to, not that it equals a particular string.
    declared = providers.model(resolve(REQUIREMENTS_TASK).model)
    assert declared is not None
    assert REQUIREMENTS_TASK.resolved_effort() in declared.reasoning.accepts


def test_alembic_has_exactly_one_head():
    """Two sessions wrote migrations against the same parent and merged within
    minutes. Neither was wrong and they touched different tables, but alembic
    cannot pick between two heads - `upgrade head` fails outright, so nothing
    migrates and every deploy stops.

    Parallel work makes this a recurring hazard rather than a one-off, and it
    is invisible until something tries to migrate."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"{len(heads)} alembic heads: {heads}; add a merge revision"


def test_no_fleet_task_defaults_to_a_model_that_is_dropping_requests():
    """gpt-5-mini fails 1.6% of its requests - 499 of 31,999 - against zero for
    both other models across 80,000. Nothing was watching, because a batch that
    returns fewer lines than it was sent looks like a batch.

    Asserting on the defaults rather than on behaviour: a task pointed at a
    failing model works fine most of the time, which is exactly why this needs
    to be a fact about the configuration."""
    from api.routers.filters import IMPROVE_MODEL
    from api.tasks.mail_classify import BACKFILL_MODEL, ONGOING_MODEL
    from api.tasks.requirements import REQUIREMENTS_MODEL

    for name, model in (
        ("requirements", REQUIREMENTS_MODEL),
        ("mail backfill", BACKFILL_MODEL),
        ("mail ongoing", ONGOING_MODEL),
        ("improve prompt", IMPROVE_MODEL),
    ):
        assert model != "gpt-5-mini", f"{name} still defaults to gpt-5-mini"


def test_swapping_the_mail_or_requirements_model_carries_its_effort():
    """luna REJECTS "minimal" and mini rejects "none", so a literal effort makes
    the model unswappable - point a task at the other one and resolve() refuses,
    or a batch submits and fails whole on a 400. That is #179, and it broke the
    ongoing mail path while the backfill kept working."""
    from api.tasks.mail_classify import BACKFILL_TASK, ONGOING_TASK
    from api.tasks.requirements import REQUIREMENTS_TASK
    from core import providers
    from core.routing import resolve

    for shape in (REQUIREMENTS_TASK, BACKFILL_TASK, ONGOING_TASK):
        chosen = resolve(shape)
        declared = providers.model(chosen.model)
        assert declared is not None
        effort = shape.resolved_effort()
        assert effort not in declared.reasoning.rejects, (
            f"{chosen.model} rejects {effort!r}; a batch would fail whole"
        )
