"""The drift check must be able to fail, and these are the ways it has to.

`scripts/measure_profile.py --check` is what stops the corpus quietly becoming
an assumption again. A check that cannot fail is worse than no check, because
a green schedule reads as "production still matches" when it means "nothing
was compared". Each case here is a shape production could grow tomorrow that
the generator could not produce.

Hermetic: `drift()` compares two profile dictionaries and touches nothing.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_measure_profile():
    """scripts/ is not a package, so it is loaded by path. Importing the real
    module rather than re-stating its logic is the point: a test against a
    second copy of drift() would pass while the shipped one was broken."""
    spec = importlib.util.spec_from_file_location(
        "measure_profile", ROOT / "scripts" / "measure_profile.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mp = _load_measure_profile()


@pytest.fixture
def recorded() -> dict:
    """The committed profile, which is a real measurement of production rather
    than a hand-written sample. A synthetic fixture here would only prove that
    drift() agrees with whatever this file made up."""
    return json.loads((ROOT / "tests" / "production_profile.json").read_text())


def test_an_unchanged_profile_reports_nothing(recorded):
    """The other direction. If this ever fails the check is crying wolf, and a
    check that cries wolf on a schedule gets muted within a fortnight."""
    assert mp.drift(recorded, copy.deepcopy(recorded)) == []


def test_a_table_added_to_production_fails_the_check(recorded):
    """The failure `sync_testdb.py` had for eleven tables and 345,032 rows: a
    table appears and nothing anywhere says so."""
    current = copy.deepcopy(recorded)
    current["tables"]["a_table_nobody_profiled"] = {"rows": 1, "sampled": 1, "columns": {}}
    findings = mp.drift(recorded, current)
    assert any("a_table_nobody_profiled" in f for f in findings), findings


def test_a_column_added_to_production_fails_the_check(recorded):
    current = copy.deepcopy(recorded)
    current["tables"]["jobs"]["columns"]["comp_equity"] = {
        "kind": "numeric",
        "null_rate": 0.5,
        "quantiles": [0.0, 1.0],
    }
    findings = mp.drift(recorded, current)
    assert any("jobs.comp_equity" in f for f in findings), findings


def test_a_new_categorical_value_fails_the_check(recorded):
    """The one that matters most. A `status` the corpus cannot hold is a code
    path no generated row will ever reach."""
    current = copy.deepcopy(recorded)
    current["tables"]["ai_queries"]["columns"]["status"]["values"]["deferred"] = 0.01
    findings = mp.drift(recorded, current)
    assert any("deferred" in f for f in findings), findings


def test_a_new_value_inside_a_partition_fails_the_check(recorded):
    """ai_queries.reason is measured per check_type, because unconditionally it
    is 20,151 strings of free text and conditioned on 'content' it is the three
    values the ATS collapse detector divides by."""
    current = copy.deepcopy(recorded)
    part = current["tables"]["ai_queries"]["columns"]["reason"]["parts"]["content"]
    part["values"]["rendered by the browser"] = 0.01
    findings = mp.drift(recorded, current)
    assert any("rendered by the browser" in f for f in findings), findings


def test_a_column_that_starts_holding_nulls_fails_the_check(recorded):
    current = copy.deepcopy(recorded)
    current["tables"]["jobs"]["columns"]["company"]["null_rate"] = 0.02
    findings = mp.drift(recorded, current)
    assert any("jobs.company" in f and "nulls" in f for f in findings), findings


def test_a_numeric_range_moving_past_the_profile_fails_the_check(recorded):
    """The weekly-pay case, generalised: comp_min's floor is the shape that
    made the column unsortable, and a corpus built to the old floor cannot
    produce a new one."""
    current = copy.deepcopy(recorded)
    quantiles = current["tables"]["jobs"]["columns"]["comp_min"]["quantiles"]
    current["tables"]["jobs"]["columns"]["comp_min"]["quantiles"] = [600.0, *quantiles[1:]]
    findings = mp.drift(recorded, current)
    assert any("jobs.comp_min" in f and "range moved" in f for f in findings), findings


def test_timestamps_reverting_to_naive_local_time_fails_the_check(recorded):
    """It was naive for months and shifted every window query by the writer's
    offset. Nothing in the corpus can reproduce it, so the check has to see
    it."""
    current = copy.deepcopy(recorded)
    current["tables"]["ai_queries"]["columns"]["created_at"]["naive_rate"] = 0.3
    findings = mp.drift(recorded, current)
    assert any("naive local time" in f for f in findings), findings


def test_a_value_production_stops_writing_is_not_drift(recorded):
    """Deliberately one-directional. The corpus keeping a shape production has
    dropped costs nothing, and failing on it would mean re-measuring after
    every quiet week until the check means nothing."""
    current = copy.deepcopy(recorded)
    values = current["tables"]["ai_queries"]["columns"]["status"]["values"]
    values.pop("failed")
    assert mp.drift(recorded, current) == []


def test_the_committed_profile_carries_nothing_identifying():
    """The profile is committed to the repository, so this is the whole safety
    argument for measuring production at all. Checked against the file rather
    than against the rules that produced it, because the rules are what would
    be wrong."""
    import re

    text = (ROOT / "tests" / "production_profile.json").read_text()
    for pattern, what in (
        (r"[\w.+-]+@[\w-]+\.\w{2,}", "an email address"),
        (r"https?://", "a URL"),
        # Long AND high-entropy. Plain length also matches a settings key like
        # 'when_dropped_as_not_job_related', which is a sentence, not a secret.
        (r"\b(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{20,}\b", "a token"),
        (r"\b[0-9a-f]{32,}\b", "a hex digest"),
    ):
        found = re.findall(pattern, text)
        assert not found, f"the profile carries {what}: {sorted(set(found))[:5]}"
