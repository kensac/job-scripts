"""Reason group classification and SQL equivalence."""

from api import db
from core.reason_taxonomy import (
    EVIDENCE_MISSING_SQL,
    GROUP_KEYS,
    classify,
    is_evidence_missing,
    sql_pattern,
)
from tests.filter_insights_support import _reject


def test_near_duplicate_phrasings_land_in_one_group():
    """The whole point of grouping: these are the same complaint written three
    ways, and an aggregate that keeps them apart says nothing."""
    for text in (
        "Salary not disclosed; cannot confirm $150k+ tier for new grads.",
        "Pay not listed; cannot confirm $150k+ total compensation for new grads.",
        "Compensation not stated; unable to confirm the pay tier.",
    ):
        assert "pay_undisclosed" in classify(text), text


def test_classification_is_multi_label():
    """A reason usually cites more than one thing. Forcing a single label would
    silently drop the other half of why a job was rejected."""
    keys = classify(
        "Senior Data Architect requiring 16 years experience; not appropriate "
        "for first-year new grad, plus no pay information."
    )
    assert "seniority" in keys
    assert "pay_undisclosed" in keys


def test_absent_pay_is_distinguished_from_stated_low_pay():
    """The distinction the whole feature exists to surface: a filter rejecting
    because the posting withheld a number is misfiring; one rejecting because
    the number was stated and too low is working."""
    absent = "No pay disclosed; unlikely to reach $200k first-year TC."
    stated = "Base salary range $66k-$82k; well below the $150k top-tier target."

    assert "pay_undisclosed" in classify(absent)
    assert is_evidence_missing(absent)

    assert "pay_below" in classify(stated)
    assert not is_evidence_missing(stated), "a disclosed number is not missing evidence"


def test_experience_ranges_match_with_either_dash():
    # The model writes both; an en dash slipping through unmatched would have
    # quietly undercounted the largest group in the corpus.
    assert "seniority" in classify("requires 2-5 years experience")
    assert "seniority" in classify("requires 2\u20135 years experience")


def test_unmatched_reason_classifies_as_nothing_rather_than_guessing():
    # Assigning a real reason to no group is the honest outcome; the residual
    # is reported rather than forced into the nearest-looking bucket.
    assert classify("Role is regulatory/compliance work.") == ()
    assert classify("") == ()
    assert classify(None) == ()


# A sample drawn from the production corpus, kept deliberately varied in
# phrasing rather than picked to pass. Coverage over the full 14,084 rejections
# measured 95.4%; this floor is what stops an edit to the patterns regressing
# that without anyone noticing.
_CORPUS_SAMPLE = (
    "Non-engineering trade role (plumbing technician); does not match software/backend criteria.",
    "Senior Data Architect requiring 16 years experience; not appropriate for new grad.",
    "Base salary tops at $65k; no equity/bonus; does not meet $200k first-year TC bar.",
    "No pay disclosed; HR services firm typically pays well below $200k first-year TC.",
    "Low overlap; role focuses on occupational safety data analysis.",
    "Role is IT/BA-focused with end-user support; lacks core backend or cloud infra overlap.",
    "Job requires PhD; candidate's education shows BS, no PhD.",
    "Role is data conversion/analyst, not engineering (backend/infrastructure).",
    "Hourly pay $18 translates to ~$37k/year; not plausibly $200k+ TC for a new grad.",
    "Bar A fail: starting salary $108k; unclear total comp to reach $200k.",
    "2+ years experience requirement for Product Owner; compensation under $150k.",
    "BAR B fails: role is hardware silicon (ASIC/RTL/FPGA); candidate is software-focused.",
    "No pay stated and consumer networking vendor unlikely to pay $200k+ for entry hardware design.",
    "Postdoc role requires PhD; pay range $72,879-$121,465 is below $150k top tier.",
    "Company not in elite tier; data annotation role is not a top-tier engineering position.",
    "Salary range $66k-$82k; well below the $150k top-tier target.",
    "Top base $85k (<$150k); BAR A fails.",
    "BAR A fails: top base $121,600 < $150k threshold for clearing pay.",
    "Non-tech civil construction role; not US-based or remote, outside user criteria.",
    "Employer is a staffing/recruiting agency, not a direct tech company.",
    "Base pay is $21-$23/hr, well below $150k/year top tier for new grads.",
    "Pay info missing; cannot confirm BAR A threshold of $200k.",
    "Role centers on medical device QA/hardware testing; lacks software overlap.",
    "Non-software/engineering role (assembly/test technician); not aligned with candidate profile.",
    "Outside US, onsite role; non-software position.",
)


def test_reason_taxonomy_coverage_on_representative_sample():
    placed = [r for r in _CORPUS_SAMPLE if classify(r)]
    coverage = len(placed) / len(_CORPUS_SAMPLE)
    assert coverage >= 0.90, (
        f"taxonomy placed only {coverage:.0%} of the sample; "
        f"unplaced: {[r for r in _CORPUS_SAMPLE if not classify(r)]}"
    )


def test_every_group_key_is_reachable_from_the_sample():
    """A pattern nothing can match is dead weight that still reads as a real
    category to whoever renders it."""
    seen = {k for r in _CORPUS_SAMPLE for k in classify(r)}
    assert set(GROUP_KEYS) - seen == set()


def test_sql_patterns_select_exactly_what_classify_labels(client, admin_headers):
    """The drill-through filters in SQL while the aggregate classifies in
    Python. If those two ever disagree, a count links to a different set of
    rows than it counted, which is worse than not linking at all."""
    for i, reason in enumerate(_CORPUS_SAMPLE):
        _reject(f"https://j.test/{i}", reason, "hash_aaaa", "f")

    for key in GROUP_KEYS:
        in_python = {
            f"https://j.test/{i}" for i, r in enumerate(_CORPUS_SAMPLE) if key in classify(r)
        }
        in_sql = {
            row["url"]
            for row in db.query(
                "SELECT url FROM ai_queries WHERE reason ~* %s", (sql_pattern(key),)
            )
        }
        assert in_sql == in_python, key

    missing_python = {
        f"https://j.test/{i}" for i, r in enumerate(_CORPUS_SAMPLE) if is_evidence_missing(r)
    }
    missing_sql = {
        row["url"]
        for row in db.query(
            "SELECT url FROM ai_queries WHERE reason ~* %s", (EVIDENCE_MISSING_SQL,)
        )
    }
    assert missing_sql == missing_python


def test_sql_pattern_rejects_an_unknown_key():
    import pytest

    with pytest.raises(KeyError):
        sql_pattern("no_such_group")
