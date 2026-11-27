"""Measure one AI step across models and efforts from a checkout, without the
fleet: the agent's way to run an experiment.

Requests are built by the step declarations in `api.experiments`, which use
the production recipes and input caps, so a run on a branch measures the
branch's prompt and a run on main measures main's. The database is only read
(the sample and what production decided), so this can point at production.
Answers, usage and cost go to files in --out; nothing is written to any
database. Without --submit nothing is sent: the run prints the requests it
would make and what they would cost at most. See docs/agents/observability.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any


def _arm(text: str) -> tuple[str, str]:
    model, sep, effort = text.rpartition("@")
    if not sep or not model or not effort:
        raise argparse.ArgumentTypeError("an arm is model@effort")
    return model, effort


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="sample, build requests, and (with --submit) send them")
    run.add_argument("--step", required=True, help="filter, verify, comp or requirements")
    run.add_argument("--arm", action="append", type=_arm, required=True, help="model@effort")
    run.add_argument("--sample", type=int, default=100)
    run.add_argument("--seed", required=True, help="the same seed draws the same postings")
    run.add_argument("--filter-id", type=int, help="the filter step's user_filters id")
    run.add_argument("--reference", help="arm every other arm is scored against")
    run.add_argument("--label", help="names this checkout, e.g. main or the branch")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--submit", action="store_true", help="send the requests (spends money)")
    run.add_argument(
        "--max-usd", type=float, default=0.5, help="refuse a run whose bound exceeds this"
    )
    score = sub.add_parser("score", help="score one or more finished runs together")
    score.add_argument("runs", nargs="+", type=Path)
    score.add_argument("--reference")
    args = parser.parse_args(argv)
    if args.command == "score":
        summary = _score(args.runs, args.reference)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    if not 1 <= args.sample <= 2000 or len(args.arm) > 16:
        parser.error("sample is 1 to 2000 postings and at most 16 arms")
    return asyncio.run(_run(args))


def _bound_cost(
    arms: list[tuple[str, str]],
    instructions: str,
    inputs: dict[str, str],
    max_output_tokens: int,
) -> tuple[float | None, str | None]:
    """Upper-bound batch cost assuming full output caps and the batch token estimate."""
    from core import pricing
    from core.batch import BATCH_CHARS_PER_TOKEN

    bound = 0.0
    for model, _ in arms:
        for text in inputs.values():
            cost = pricing.estimate_cost_usd(
                model,
                (len(instructions) + len(text)) // BATCH_CHARS_PER_TOKEN,
                max_output_tokens,
                batched=True,
            )
            if cost is None:
                return None, model
            bound += float(cost)
    return bound, None


def _collect_arm_results(
    step: Any,
    arms: list[tuple[str, str]],
    answered: list[dict[str, Any]],
    label: str | None,
) -> list[dict[str, Any]]:
    from api import experiments as exp
    from core import pricing

    results = []
    for (model, effort), by_url in zip(arms, answered, strict=True):
        arm = exp.arm_name(model, effort, label)
        for url, res in by_url.items():
            usage = exp.usage(res)
            cost = pricing.estimate_cost_usd(
                model,
                usage["input_tokens"],
                usage["output_tokens"],
                cached_tokens=usage["cached_tokens"],
                cache_write_tokens=usage["cache_write_tokens"],
                batched=True,
            )
            output, error = None, res.error
            if res.text and not res.error:
                try:
                    output = step.answer_model.model_validate_json(res.text).model_dump(mode="json")
                except ValueError as e:
                    error = f"unparsable: {str(e)[:200]}"
            results.append(
                {
                    "arm": arm,
                    "url": url,
                    "output": output,
                    "usage": usage,
                    "cost_usd": float(cost) if cost is not None else None,
                    "error": error,
                }
            )
    return results


def _write_run_artifacts(
    out_dir: Path,
    step_name: str,
    params: dict[str, Any],
    results: list[dict[str, Any]],
    reference: str | None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps({"purpose": step_name, "params": params}))
    with (out_dir / "results.jsonl").open("w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    summary = _score([out_dir], reference)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


async def _run(args: argparse.Namespace) -> int:
    from api import experiments as exp
    from core.batch import run_responses_batch, structured_response_spec

    step = exp.steps().get(args.step)
    if step is None:
        print(f"measurable steps: {sorted(exp.steps())}", file=sys.stderr)
        return 2
    arms = [(m, e) for m, e in args.arm]
    skipped = {exp.arm_name(m, e): why for m, e in arms if (why := exp.arm_ok(m, e))}
    arms = [(m, e) for m, e in arms if exp.arm_name(m, e) not in skipped]
    if not arms:
        print(json.dumps({"no_arm_can_run": skipped}), file=sys.stderr)
        return 2
    params: dict[str, Any] = {
        "sample": args.sample,
        "seed": args.seed,
        "arms": [{"model": m, "effort": e, "label": args.label} for m, e in arms],
        "filter_id": args.filter_id,
        "reference": args.reference,
        "skipped": skipped,
    }
    instructions = step.instructions(params)
    rows = exp.sample(args.sample, args.seed)
    if not rows:
        print("no eligible postings to sample", file=sys.stderr)
        return 2
    params["sampled"] = len(rows)
    inputs = {r["url"]: step.build_input(r) for r in rows}
    lengths = [len(i) for i in inputs.values()]
    bound, unpriced_model = _bound_cost(arms, instructions, inputs, step.max_output_tokens)
    if bound is None:
        print(f"{unpriced_model} has no published price; spend cannot be bounded", file=sys.stderr)
        return 2
    plan = {
        "step": args.step,
        "arms": [exp.arm_name(m, e, args.label) for m, e in arms],
        "skipped": skipped,
        "postings": len(rows),
        "requests": len(rows) * len(arms),
        # Check input_chars before submitting to catch truncated or header-only inputs.
        "input_chars": {
            "min": min(lengths),
            "median": int(statistics.median(lengths)),
            "max": max(lengths),
        },
        "max_cost_usd": round(bound, 4),
    }
    print(json.dumps(plan, indent=2))
    if not args.submit:
        return 0
    if bound > args.max_usd:
        print(f"bound ${bound:.4f} exceeds --max-usd {args.max_usd}", file=sys.stderr)
        return 2

    specs = [
        structured_response_spec(r["url"], instructions, inputs[r["url"]], step.answer_model)
        for r in rows
    ]
    answered = await asyncio.gather(
        *(run_responses_batch(specs, m, e, step.max_output_tokens) for m, e in arms)
    )
    results = _collect_arm_results(step, arms, answered, args.label)
    summary = _write_run_artifacts(args.out, args.step, params, results, args.reference)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def _score(runs: list[Path], reference: str | None) -> dict[str, Any]:
    """Score runs together: the same seed and sample on main and on a branch
    is the before and after of a prompt change."""
    from api import experiments as exp

    loaded = [json.loads((d / "run.json").read_text()) for d in runs]
    purposes = {r["purpose"] for r in loaded}
    if len(purposes) != 1:
        raise SystemExit(f"runs measure different steps: {sorted(purposes)}")
    sampled = {r["params"]["sampled"] for r in loaded}
    params = {
        **loaded[0]["params"],
        "arms": [a for r in loaded for a in r["params"]["arms"]],
        "skipped": {k: v for r in loaded for k, v in r["params"]["skipped"].items()},
        "sampled": sampled.pop() if len(sampled) == 1 else None,
        "reference": reference or loaded[0]["params"].get("reference"),
    }
    rows = [json.loads(line) for d in runs for line in (d / "results.jsonl").open()]
    return exp.score(purposes.pop(), params, rows)


if __name__ == "__main__":
    # Before the pool is imported: this tool never writes, so a mistake in it
    # cannot write either, wherever DATABASE_URL points.
    os.environ["PGOPTIONS"] = (
        os.environ.get("PGOPTIONS", "")
        + " -c default_transaction_read_only=on -c statement_timeout=300s"
        + " -c application_name=run_experiment"
    )
    raise SystemExit(main())
