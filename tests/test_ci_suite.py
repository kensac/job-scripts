from __future__ import annotations

import json

import pytest

pytest_plugins = ["pytester"]


def test_shards_partition_selected_tests_without_losing_cases(pytester):
    pytester.makeini("[pytest]\nmarkers = corpus: generated database")
    pytester.makepyfile(
        test_sample="""
        import pytest
        @pytest.mark.parametrize('value', range(12))
        def test_ordinary(value):
            assert value >= 0
        @pytest.mark.corpus
        def test_catalog():
            pass
        """
    )
    selected = []
    for index in range(3):
        output = pytester.path / f"shard-{index}.json"
        result = pytester.runpytest(
            "-p",
            "tests.ci_suite",
            "-m",
            "not corpus",
            "--shard-count=3",
            f"--shard-index={index}",
            f"--test-report={output}",
        )
        assert result.ret == 0
        report = json.loads(output.read_text())
        selected.extend(report["selected"])
    expected = {f"test_sample.py::test_ordinary[{value}]" for value in range(12)}
    assert len(selected) == len(set(selected)) == 12
    assert set(selected) == expected


def test_invalid_shard_refuses_to_run(pytester):
    pytester.makepyfile("def test_ok(): pass")
    result = pytester.runpytest("-p", "tests.ci_suite", "--shard-count=2", "--shard-index=2")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--shard-index must be between 0 and*", "*"])


def test_report_preserves_failures_skips_and_phase_durations(pytester):
    pytester.makepyfile(
        test_sample="""
        import pytest
        def test_fails():
            assert False
        @pytest.mark.skip(reason='deliberate')
        def test_skips():
            pass
        """
    )
    output = pytester.path / "timing.json"
    result = pytester.runpytest("-p", "tests.ci_suite", f"--test-report={output}")
    result.assert_outcomes(failed=1, skipped=1)
    report = json.loads(output.read_text())
    assert report["exitstatus"] == 1
    assert set(report["selected"]) == {"test_sample.py::test_fails", "test_sample.py::test_skips"}
    failure = report["reports"]["test_sample.py::test_fails"]
    assert any(r["phase"] == "call" and r["outcome"] == "failed" for r in failure)
    assert {r["phase"] for r in failure} == {"setup", "call", "teardown"}
    assert all(r["seconds"] >= 0 for rows in report["reports"].values() for r in rows)
    assert report["pytest_seconds"] > 0


@pytest.mark.parametrize(
    "defect", ["missing", "duplicate", "outcome", "revision", "failed", "unreported"]
)
def test_comparison_rejects_incomplete_or_changed_coverage(defect):
    from tools.test_performance import compare

    def report(lane, selected):
        return {
            "revision": "same-revision",
            "repetition": 1,
            "lane": lane,
            "exitstatus": 0,
            "pytest_seconds": 1,
            "selected": selected,
            "reports": {
                nodeid: [{"phase": "call", "outcome": "passed", "subtest": False}]
                for nodeid in selected
            },
        }

    baseline = report("serial", ["a", "b"])
    shard = report("shard-0", ["a"])
    corpus = report("corpus", ["b"])
    if defect == "missing":
        corpus = report("corpus", [])
    elif defect == "duplicate":
        corpus = report("corpus", ["a", "b"])
    elif defect == "outcome":
        corpus["reports"]["b"][0]["outcome"] = "skipped"
    elif defect == "revision":
        corpus["revision"] = "different-revision"
    elif defect == "failed":
        corpus["exitstatus"] = 1
    elif defect == "unreported":
        corpus["reports"] = {}
    with pytest.raises(ValueError):
        compare([baseline, shard, corpus], repetitions=1, shards=1)


def test_manifest_requires_complete_same_revision_execution(pytester, monkeypatch):
    from tools.test_performance import verify_manifest

    monkeypatch.setenv("TEST_REVISION", "manifest-revision")
    monkeypatch.setenv("TEST_LANE", "shard-0")
    monkeypatch.setenv("TEST_REPETITION", "1")
    pytester.makepyfile(test_sample="def test_ok(): pass")
    manifest_path = pytester.path / "manifest.json"
    execution_path = pytester.path / "timing.json"
    assert (
        pytester.runpytest(
            "-p", "tests.ci_suite", "--collect-only", f"--test-report={manifest_path}"
        ).ret
        == 0
    )
    assert pytester.runpytest("-p", "tests.ci_suite", f"--test-report={execution_path}").ret == 0
    manifest = json.loads(manifest_path.read_text())
    execution = json.loads(execution_path.read_text())
    empty_corpus = dict(execution, lane="corpus", selected=[], reports={})
    reports = [execution, empty_corpus]
    assert verify_manifest(manifest, reports, shards=1) == {"test_sample.py::test_ok": "passed"}
    with pytest.raises(ValueError, match="lanes"):
        verify_manifest(manifest, [execution], shards=1)
    manifest["revision"] = "stale-revision"
    with pytest.raises(ValueError, match="revisions"):
        verify_manifest(manifest, reports, shards=1)
    manifest["revision"] = execution["revision"]
    manifest["selected"].append("missing.py::test_missing")
    with pytest.raises(ValueError, match="partition"):
        verify_manifest(manifest, reports, shards=1)
