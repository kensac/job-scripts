"""The derived-fact registry: every derivation is one entry the worker both
schedules and dispatches, switched-off ones included."""

from api import db
from tasks import DERIVATIONS, HANDLERS, derive
from tasks.requirements import REQUIREMENTS


def _kinds() -> list[str]:
    return sorted(r["kind"] for r in db.query("SELECT kind FROM tasks"))


def test_every_derivation_is_dispatched_to_the_shared_sweep():
    kinds = [d.kind for d in DERIVATIONS]
    assert len(set(kinds)) == len(kinds)
    for d in DERIVATIONS:
        assert HANDLERS[d.kind] == d.handle
        assert d.shape is not None or d.model is not None, d.kind
        assert not d.skip_unchanged or d.reads_page, d.kind


def test_a_switched_off_derivation_is_registered_but_never_scheduled(set_config):
    # Stated rather than left to the seeded defaults: both features are off.
    set_config("requirements_extraction_enabled", False)
    set_config("job_profile_collection_enabled", False)
    assert {"extract_requirements", "classify_job_profiles"} <= {d.kind for d in DERIVATIONS}

    for d in DERIVATIONS:
        derive.schedule(d, "c1")
    assert _kinds() == ["classify_locations", "embed_postings_batch", "extract_comp"]

    # A pass still in flight blocks the next cycle's.
    for d in DERIVATIONS:
        derive.schedule(d, "c2")
    assert len(_kinds()) == 3

    set_config("requirements_extraction_enabled", True)
    derive.schedule(REQUIREMENTS, "c3")
    assert "extract_requirements" in _kinds()
