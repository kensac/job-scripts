from __future__ import annotations

import argparse
import collections
import json
import pstats
import statistics
from pathlib import Path


def outcomes(report):
    result = {}
    for nodeid in report["selected"]:
        rows = report["reports"].get(nodeid, [])
        main = [row for row in rows if not row["subtest"]]
        if any(row["outcome"] == "failed" for row in rows):
            result[nodeid] = "failed"
        elif any(row["phase"] == "call" and row["outcome"] == "passed" for row in main):
            result[nodeid] = "passed"
        elif any(row["outcome"] == "skipped" for row in main):
            result[nodeid] = "skipped"
        else:
            raise ValueError(f"No completed outcome for {nodeid}")
    return result


def index_reports(reports):
    revisions = {report["revision"] for report in reports}
    if len(revisions) != 1 or None in revisions:
        raise ValueError("All reports must identify the same tested revision")
    indexed = {}
    for report in reports:
        key = (report["repetition"], report["lane"])
        if key in indexed:
            raise ValueError(f"Duplicate report for {key}")
        if report["exitstatus"] != 0:
            raise ValueError(f"Test run failed: {key}")
        if len(report["selected"]) != len(set(report["selected"])):
            raise ValueError(f"Duplicate selected cases in {key}")
        if set(report["selected"]) != set(report["reports"]):
            raise ValueError(f"Selected and executed cases disagree in {key}")
        indexed[key] = report
    return indexed


def partition(selected, reports):
    counts = collections.Counter(nodeid for report in reports for nodeid in report["selected"])
    if counts != collections.Counter(selected):
        raise ValueError("Parallel cases must partition the baseline exactly once")
    return {nodeid: outcome for report in reports for nodeid, outcome in outcomes(report).items()}


def verify_manifest(manifest, reports, *, shards):
    indexed = index_reports(reports)
    if manifest["exitstatus"] != 0 or not manifest["selected"]:
        raise ValueError("A successful nonempty collection manifest is required")
    if any(report["revision"] != manifest["revision"] for report in reports):
        raise ValueError("Collection and execution revisions differ")
    expected = {(1, f"shard-{index}") for index in range(shards)} | {(1, "corpus")}
    if set(indexed) != expected:
        raise ValueError("All configured lanes must report exactly once")
    return partition(manifest["selected"], reports)


def compare(reports, *, repetitions, shards):
    indexed = index_reports(reports)
    comparisons = []
    for repetition in range(1, repetitions + 1):
        baseline = indexed[(repetition, "serial")]
        parallel = [indexed[(repetition, f"shard-{index}")] for index in range(shards)]
        parallel.append(indexed[(repetition, "corpus")])
        parallel_outcomes = partition(baseline["selected"], parallel)
        baseline_outcomes = outcomes(baseline)
        if baseline_outcomes != parallel_outcomes:
            raise ValueError("Serial and parallel outcomes differ")
        comparisons.append(
            {
                "repetition": repetition,
                "tests": len(parallel_outcomes),
                "outcomes": dict(collections.Counter(baseline_outcomes.values())),
                "serial_seconds": baseline["pytest_seconds"],
                "parallel_seconds": max(report["pytest_seconds"] for report in parallel),
                "parallel_total_seconds": sum(report["pytest_seconds"] for report in parallel),
            }
        )
    return comparisons


def main():
    parser = argparse.ArgumentParser(description="Compare complete, same-revision test runs")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument(
        "--manifest", type=Path, help="verify CI execution against complete collection"
    )
    args = parser.parse_args()
    reports = [json.loads(path.read_text()) for path in args.directory.rglob("timing.json")]
    if args.manifest:
        result = verify_manifest(json.loads(args.manifest.read_text()), reports, shards=args.shards)
        print(
            f"Verified {len(result)} cases executed exactly once: {dict(collections.Counter(result.values()))}"
        )
        return
    if not args.repetitions or args.repetitions < 1:
        parser.error("--repetitions must be positive for a benchmark comparison")
    comparisons = compare(reports, repetitions=args.repetitions, shards=args.shards)
    print("# Test performance comparison\n")
    print("All parallel test IDs and outcomes match the serial run exactly.\n")
    print("| Repeat | Cases | Serial pytest | Parallel critical path | Parallel total |")
    print("|---|---:|---:|---:|---:|")
    for row in comparisons:
        print(
            f"| {row['repetition']} | {row['tests']} | {row['serial_seconds']:.2f}s | {row['parallel_seconds']:.2f}s | {row['parallel_total_seconds']:.2f}s |"
        )
    print(
        "\nThese times cover pytest startup and execution, excluding runner provisioning, dependency installation and artifact upload. Parallel critical path assumes jobs are available together; workflow/job timestamps must also be compared.\n"
    )
    for report in reports:
        if report["lane"] != "serial":
            continue
        phases = collections.defaultdict(float)
        for rows in report["reports"].values():
            for row in rows:
                if not row["subtest"]:
                    phases[row["phase"]] += row["seconds"]
        print(f"## Serial repetition {report['repetition']}\n")
        print(
            "Phase totals: "
            + ", ".join(f"{name} {seconds:.2f}s" for name, seconds in sorted(phases.items()))
            + ".\n"
        )
        for phase in ("setup", "call"):
            ranked = sorted(
                (
                    (
                        sum(
                            row["seconds"]
                            for row in rows
                            if row["phase"] == phase and not row["subtest"]
                        ),
                        nodeid,
                    )
                    for nodeid, rows in report["reports"].items()
                ),
                reverse=True,
            )
            print(f"Slowest {phase} phases:\n")
            for seconds, nodeid in ranked[:10]:
                print(f"- {seconds:.3f}s: `{nodeid}`")
            print()
    for path in args.directory.rglob("profile.pstats"):
        stats = pstats.Stats(str(path))
        print("## Diagnostic call profile\n")
        print(
            "This separate instrumented run is excluded from the speed comparison. Cumulative function times overlap and must not be added together.\n"
        )
        rows = [
            (values[3], values[1], file, name)
            for (file, _line, name), values in stats.stats.items()
            if file.endswith(("tests/conftest.py", "tests/corpus.py", "src/api/db.py"))
        ]
        for seconds, calls, file, name in sorted(rows, reverse=True)[:20]:
            print(f"- {seconds:.3f}s cumulative, {calls} calls: `{Path(file).name}:{name}`")
    serial = [row["serial_seconds"] for row in comparisons]
    parallel = [row["parallel_seconds"] for row in comparisons]
    print(
        f"\nMedian across {len(comparisons)} repetitions: serial {statistics.median(serial):.2f}s; parallel critical path {statistics.median(parallel):.2f}s."
    )


if __name__ == "__main__":
    main()
