"""Measure one AI step across models and efforts, on a sample, through the
production path.

The 2026-09-06 filter comparison was a hand export and a script, and the
script fed every request the posting's first line; its headline was wrong
and was nearly acted on. The step declarations here build each request with
the step's own instructions, schema and input, and `score` reports per arm
(model and effort): cost, tokens, parse failures, and agreement with a
reference arm and with what production decided. The sample is drawn by seed,
so the same postings can be re-measured later. `api.run_experiment` is the
only caller.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from api import db
from core import providers, verdict_reads
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL

Params = dict[str, Any]
Posting = dict[str, Any]
Comparison = dict[str, Any]
Deployed = dict[str, Comparison]


@dataclass(frozen=True, slots=True)
class ExperimentStep:
    instruction_builder: Callable[[Params], str]
    answer_model: type[BaseModel]
    input_builder: Callable[[Posting], str]
    max_output_tokens: int
    comparison_projector: Callable[[Comparison], Comparison]
    load_deployed: Callable[[list[str], Params], Deployed]

    def instructions(self, params: Params) -> str:
        return self.instruction_builder(params)

    def build_input(self, posting: Posting) -> str:
        return self.input_builder(posting)

    def project(self, answer: Comparison) -> Comparison:
        return self.comparison_projector(answer)


def _filter_instructions(params: dict[str, Any]) -> str:
    from core.filters import build_custom_decision_instructions

    row = db.query_one(
        "SELECT prompt, on_ambiguous FROM user_filters WHERE id = %s", (params.get("filter_id"),)
    )
    if not row:
        raise LookupError("experiment on filter needs filter_id of an existing filter")
    return build_custom_decision_instructions(row["prompt"], row["on_ambiguous"])


def _posting_input(r: dict[str, Any]) -> str:
    from core.filters import build_custom_input

    return build_custom_input(r["company"], r["title"], r["input_content"])


def _filter_fields(p: dict[str, Any]) -> dict[str, Any]:
    return {"should_filter": p.get("should_filter")}


def _verify_fields(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "is_closed": p.get("is_closed"),
        "requires_clearance_or_restrictions": p.get("requires_clearance_or_restrictions"),
    }


def _comp_fields(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "has_comp": p.get("has_comp"),
        "comp_min": p.get("comp_min"),
        "comp_max": p.get("comp_max"),
        "period": p.get("period"),
    }


def _requirements_fields(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "degree_min": p.get("degree_min") or "",
        "degree_required": bool(p.get("degree_required")),
        "seniority": p.get("seniority") or "",
        "employment_type": p.get("employment_type") or "",
        "clearance": p.get("clearance") or "",
        "yoe_min": p.get("yoe_min"),
        "skills_required": sorted(s.lower() for s in (p.get("skills_required") or [])),
    }


def steps() -> Mapping[str, ExperimentStep]:
    """The measurable steps: how each builds its request and which fields of
    its answer are compared. Imported lazily so the schemas are loaded only
    when an experiment needs the declarations."""
    from core.answers import VERIFICATION_REQUEST, FilterDecision
    from core.comp import COMP_INPUT_CHARS, COMP_INSTRUCTIONS, CompExtract
    from core.requirements import (
        REQUIREMENTS_INPUT_CHARS,
        REQUIREMENTS_INSTRUCTIONS,
        RequirementsExtract,
    )
    from core.shapes import COMP_TASK, REQUIREMENTS_TASK

    return {
        "filter": ExperimentStep(
            instruction_builder=_filter_instructions,
            answer_model=FilterDecision,
            input_builder=_posting_input,
            max_output_tokens=6000,
            comparison_projector=_filter_fields,
            load_deployed=_filter_deployed,
        ),
        "verify": ExperimentStep(
            instruction_builder=lambda params: VERIFICATION_REQUEST.instructions,
            answer_model=VERIFICATION_REQUEST.response_model,
            input_builder=lambda r: VERIFICATION_REQUEST.build_input(r["input_content"]),
            max_output_tokens=VERIFICATION_REQUEST.max_output_tokens,
            comparison_projector=_verify_fields,
            load_deployed=_verify_deployed,
        ),
        "comp": ExperimentStep(
            instruction_builder=lambda params: COMP_INSTRUCTIONS,
            answer_model=CompExtract,
            input_builder=lambda r: r["input_content"][:COMP_INPUT_CHARS],
            max_output_tokens=COMP_TASK.max_output_tokens,
            comparison_projector=_comp_fields,
            load_deployed=_comp_deployed,
        ),
        "requirements": ExperimentStep(
            instruction_builder=lambda params: REQUIREMENTS_INSTRUCTIONS,
            answer_model=RequirementsExtract,
            input_builder=lambda r: r["input_content"][:REQUIREMENTS_INPUT_CHARS],
            max_output_tokens=REQUIREMENTS_TASK.max_output_tokens,
            comparison_projector=_requirements_fields,
            load_deployed=_requirements_deployed,
        ),
    }


def arm_name(model: str, effort: str, label: str | None = None) -> str:
    """`label` names the checkout a run came from, so the same arm measured
    on main and on a branch can be scored side by side."""
    return f"{label}:{model}@{effort}" if label else f"{model}@{effort}"


def arm_ok(model: str, effort: str) -> str | None:
    """Why an arm cannot run, or None. A batch fails whole on a rejected
    effort, so an arm the model refuses is dropped before submission."""
    declared = providers.model(model)
    if declared is None:
        return "unknown model"
    reasoning = declared.reasoning
    if reasoning.accepts and effort not in reasoning.accepts:
        return f"{model} does not accept effort {effort}"
    if effort in reasoning.rejects:
        return f"{model} rejects effort {effort}"
    return None


# About a third of eligible postings are verified open (2026-10-10), so one
# chunk covers a sample of a few hundred.
_SAMPLE_CHUNK = 1000


def sample(n: int, seed: str) -> list[dict[str, Any]]:
    """n verified-open postings with content, in an order fixed by the seed,
    so a later run measures the same postings.

    The draw orders the eligible postings first and checks verdicts and
    content a chunk at a time, stopping at n. One query that filtered and
    joined content before ordering read page text for every verified-open
    posting and passed a 300 s statement timeout on production (161,623 of
    481,709 eligible, 2026-10-10)."""
    order = db.query(
        f"""
        SELECT j.url FROM jobs j
        WHERE j.active AND {AI_ELIGIBLE_JOB.format(job="j")}
        ORDER BY md5(j.url || %(seed)s)
        """,
        {"seed": seed},
    )
    out: list[dict[str, Any]] = []
    for start in range(0, len(order), _SAMPLE_CHUNK):
        chunk = [r["url"] for r in order[start : start + _SAMPLE_CHUNK]]
        found = {
            r["url"]: r
            for r in db.query(
                f"""
                SELECT j.url, j.company, j.title, q.input_content
                FROM jobs j
                {CONTENT_LATERAL.format(url="j.url", columns="input_content")}
                WHERE j.url = ANY(%(urls)s) AND {verdict_reads.verified_open("j.url")}
                """,
                {"urls": chunk},
            )
        }
        out += [found[u] for u in chunk if u in found]
        if len(out) >= n:
            break
    return out[:n]


def usage(res: Any) -> dict[str, int | None]:
    u = res.usage or {}
    input_details = u.get("input_tokens_details") or {}
    return {
        "input_tokens": u.get("input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "cached_tokens": input_details.get("cached_tokens", 0) or 0,
        "cache_write_tokens": (
            input_details.get("cache_write_tokens")
            if "cache_write_tokens" in input_details
            else None
        ),
        "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
    }


def _filter_deployed(urls: list[str], params: Params) -> Deployed:
    row = db.query_one(
        "SELECT prompt_hash FROM user_filters WHERE id = %s", (params.get("filter_id"),)
    )
    if not row:
        return {}
    rows = db.query(
        verdict_reads.latest_per(
            "url", "url, status", "url = ANY(%s) AND check_type = 'custom' AND prompt_hash = %s"
        ),
        (urls, row["prompt_hash"]),
    )
    return {r["url"]: {"should_filter": r["status"] == "rejected"} for r in rows}


def _verify_deployed(urls: list[str], params: Params) -> Deployed:
    rows = db.query(
        verdict_reads.latest_per(
            "url, check_type",
            "url, check_type, status",
            "url = ANY(%s) AND check_type IN ('closed', 'clearance')",
        ),
        (urls,),
    )
    out: Deployed = {}
    for r in rows:
        key = "is_closed" if r["check_type"] == "closed" else "requires_clearance_or_restrictions"
        out.setdefault(r["url"], {})[key] = r["status"] == "rejected"
    return out


def _comp_deployed(urls: list[str], params: Params) -> Deployed:
    rows = db.query(
        "SELECT url, comp_min, comp_max, comp_period, comp_text FROM jobs "
        "WHERE url = ANY(%s) AND comp_extracted",
        (urls,),
    )
    return {
        r["url"]: {
            "has_comp": r["comp_text"] is not None,
            "comp_min": r["comp_min"],
            "comp_max": r["comp_max"],
            "period": r["comp_period"],
        }
        for r in rows
    }


def _requirements_deployed(urls: list[str], params: Params) -> Deployed:
    # The stored row carries the scalar fields; skills live in their own
    # table and are compared between arms only.
    rows = db.query(
        "SELECT url, degree_min, degree_required, seniority, employment_type, clearance, "
        "yoe_min FROM job_requirements WHERE url = ANY(%s)",
        (urls,),
    )
    return {
        r["url"]: {
            "degree_min": r["degree_min"] or "",
            "degree_required": bool(r["degree_required"]),
            "seniority": r["seniority"] or "",
            "employment_type": r["employment_type"] or "",
            "clearance": r["clearance"] or "",
            "yoe_min": r["yoe_min"],
        }
        for r in rows
    }


def deployed_verdicts(purpose: str, urls: list[str], params: Params) -> Deployed:
    """What production decided, shaped for comparison by the named step."""
    step = steps().get(purpose)
    return step.load_deployed(urls, params) if step else {}


def _agreement(a: dict[str, Any], b: dict[str, Any]) -> dict[str, float]:
    """Per-field agreement between two answers. A list field scores by
    Jaccard overlap; anything else by equality."""
    out: dict[str, float] = {}
    # Only fields both sides carry: production's requirements row has no
    # skills, and a field one side never answered is not a disagreement.
    for k in a:
        if k not in b:
            continue
        x, y = a.get(k), b.get(k)
        if isinstance(x, list) or isinstance(y, list):
            sx, sy = set(x or []), set(y or [])
            out[k] = 1.0 if not sx and not sy else len(sx & sy) / len(sx | sy)
        else:
            out[k] = 1.0 if x == y else 0.0
    return out


def score(purpose: str, params: Params, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score answers already collected: per arm cost, tokens, failures and
    agreement with the reference arm and with what production decided. Each
    row carries arm, url, output, usage, cost_usd and error."""
    step = steps()[purpose]
    by_arm: dict[str, dict[str, Any]] = {}
    fields_by_arm: dict[str, dict[str, dict]] = {}
    for r in rows:
        a = by_arm.setdefault(
            r["arm"],
            {
                "n": 0,
                "ok": 0,
                "failed": 0,
                "cost_usd": 0.0,
                "known_cost_usd": 0.0,
                "unpriced_results": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
            },
        )
        a["n"] += 1
        u = r["usage"] or {}
        a["input_tokens"] += u.get("input_tokens", 0)
        a["output_tokens"] += u.get("output_tokens", 0)
        a["reasoning_tokens"] += u.get("reasoning_tokens", 0)
        if r["cost_usd"] is None:
            a["unpriced_results"] += 1
        else:
            a["known_cost_usd"] += float(r["cost_usd"])
        if r["output"] is None:
            a["failed"] += 1
        else:
            a["ok"] += 1
            fields_by_arm.setdefault(r["arm"], {})[r["url"]] = step.project(r["output"])
    urls = sorted({r["url"] for r in rows})
    deployed = deployed_verdicts(purpose, urls, params)
    explicit_reference = params.get("reference")
    incomplete_costs = any(a["unpriced_results"] for a in by_arm.values())
    reference = explicit_reference or (
        max(by_arm, key=lambda k: by_arm[k]["known_cost_usd"])
        if by_arm and not incomplete_costs
        else None
    )
    reference_reason = "cost_incomplete" if incomplete_costs and not explicit_reference else None
    for arm, a in by_arm.items():
        n = a["n"] or 1
        known_cost = a["known_cost_usd"]
        a["cost_per_100_usd"] = (
            round(known_cost / n * 100, 4) if a["unpriced_results"] == 0 else None
        )
        a["known_cost_usd"] = round(known_cost, 4)
        a["cost_usd"] = None if a["unpriced_results"] else a["known_cost_usd"]
        a["input_per_request"] = round(a["input_tokens"] / n)
        a["output_per_request"] = round(a["output_tokens"] / n)
        a["reasoning_per_request"] = round(a["reasoning_tokens"] / n)
        mine = fields_by_arm.get(arm, {})
        for label, other in (
            ("reference", fields_by_arm.get(reference or "", {})),
            ("deployed", deployed),
        ):
            common = [u for u in mine if u in other]
            if not common:
                a[f"agreement_with_{label}"] = None
                continue
            per_field: dict[str, float] = {}
            for u in common:
                for k, v in _agreement(mine[u], other[u]).items():
                    per_field[k] = per_field.get(k, 0.0) + v
            a[f"agreement_with_{label}"] = {
                "n": len(common),
                **{k: round(v / len(common), 3) for k, v in per_field.items()},
            }
        if purpose == "filter" and mine:
            a["pass_rate"] = round(
                sum(1 for f in mine.values() if not f.get("should_filter")) / len(mine), 3
            )
    skipped = params.get("skipped") or {}
    expected_arms = {
        arm_name(arm["model"], arm["effort"], arm.get("label")) for arm in params.get("arms", [])
    } - set(skipped)
    sampled = params.get("sampled")
    expected = sampled * len(expected_arms) if sampled is not None else None
    return {
        "reference": reference,
        "reference_reason": reference_reason,
        "arms": by_arm,
        "postings": len(urls),
        "expected_results": expected,
        "received_results": len(rows),
        "missing_results": max(0, expected - len(rows)) if expected is not None else None,
        "missing_arms": sorted(expected_arms - set(by_arm)),
    }
