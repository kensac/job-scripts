"""Filter insight counts retain decisions and distinct jobs separately."""

from api.routers.filtering.insight_breakdowns import (
    PromptEvidenceAccumulator,
    ReasonBreakdownAccumulator,
    _group_rejections_by_prompt,
)


def test_reason_breakdown_counts_decisions_and_distinct_jobs():
    bucket = ReasonBreakdownAccumulator()
    bucket.record("Salary not disclosed.", "https://jobs.test/1", evidence_missing=True)
    bucket.record("No pay disclosed.", "https://jobs.test/1", evidence_missing=True)
    bucket.record("No pay disclosed.", "https://jobs.test/2", evidence_missing=True)

    breakdown = bucket.to_breakdown(examples=2)
    assert breakdown.decisions == 3
    assert breakdown.distinct_jobs == 2
    assert breakdown.distinct_phrasings == 2
    assert breakdown.evidence_missing_decisions == 3
    assert breakdown.evidence_missing_distinct_jobs == 2
    assert breakdown.examples == ["No pay disclosed.", "Salary not disclosed."]


def test_overlapping_groups_do_not_double_count_prompt_evidence():
    reason = "Requires 5 years experience and no pay disclosed."
    grouped, evidence = _group_rejections_by_prompt(
        ["prompt-a"],
        [
            {"prompt_hash": "prompt-a", "url": "https://jobs.test/1", "reason": reason},
            {"prompt_hash": "prompt-a", "url": "https://jobs.test/1", "reason": reason},
        ],
    )

    assert isinstance(evidence["prompt-a"], PromptEvidenceAccumulator)
    assert evidence["prompt-a"].decisions == 2
    assert evidence["prompt-a"].distinct_job_count == 1
    for key in ("pay_undisclosed", "seniority"):
        breakdown = grouped["prompt-a"][key].to_breakdown(2)
        assert breakdown.decisions == 2
        assert breakdown.distinct_jobs == 1
        assert breakdown.evidence_missing_decisions == 2
        assert breakdown.evidence_missing_distinct_jobs == 1
