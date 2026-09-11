"""Measure one AI step across models and efforts, on a sample, through the
production path.

The 2026-09-06 filter comparison was a hand export and a script, and the
script fed every request the posting's first line; its headline was wrong
and was nearly acted on. An experiment here builds each request with the
step's own instructions, schema and input, submits one provider batch per
arm (model and effort), and reports per arm: cost, tokens, parse failures,
and agreement with a reference arm and with what production decided. The
sample is drawn by seed, so the same postings can be re-measured later.
"""

from __future__ import annotations

from typing import Any

from api import db
from core import providers
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL, VERIFIED_OPEN

PURPOSE = "experiment"


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


def steps() -> dict[str, dict[str, Any]]:
    """The measurable steps: how each builds its request and which fields of
    its answer are compared. Imported lazily so the schemas are loaded only
    when an experiment needs the declarations."""
    from core.answers import _VERIFY_INSTRUCTIONS, FilterDecision, VerifyVerdict
    from core.comp import COMP_INSTRUCTIONS, CompExtract
    from core.requirements import (
        REQUIREMENTS_INPUT_CHARS,
        REQUIREMENTS_INSTRUCTIONS,
        RequirementsExtract,
    )
    from core.shapes import COMP_TASK, REQUIREMENTS_TASK, VERIFY_TASK

    return {
        "filter": {
            "instructions": _filter_instructions,
            "model": FilterDecision,
            "input": _posting_input,
            "max_output_tokens": 6000,
            "fields": _filter_fields,
        },
        "verify": {
            "instructions": lambda params: _VERIFY_INSTRUCTIONS,
            "model": VerifyVerdict,
            "input": lambda r: r["input_content"][:20000],
            "max_output_tokens": VERIFY_TASK.max_output_tokens,
            "fields": _verify_fields,
        },
        "comp": {
            "instructions": lambda params: COMP_INSTRUCTIONS,
            "model": CompExtract,
            "input": lambda r: r["input_content"][:20000],
            "max_output_tokens": COMP_TASK.max_output_tokens,
            "fields": _comp_fields,
        },
        "requirements": {
            "instructions": lambda params: REQUIREMENTS_INSTRUCTIONS,
            "model": RequirementsExtract,
            "input": lambda r: r["input_content"][:REQUIREMENTS_INPUT_CHARS],
            "max_output_tokens": REQUIREMENTS_TASK.max_output_tokens,
            "fields": _requirements_fields,
        },
    }


def arm_name(model: str, effort: str) -> str:
    return f"{model}@{effort}"


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


def sample(n: int, seed: str) -> list[dict[str, Any]]:
    """n verified-open postings with content, in an order fixed by the seed,
    so a later run measures the same postings."""
    return db.query(
        f"""
        SELECT j.url, j.company, j.title, q.input_content
        FROM jobs j
        {CONTENT_LATERAL.format(url="j.url", columns="input_content")}
        WHERE j.active AND {AI_ELIGIBLE_JOB.format(job="j")} AND {VERIFIED_OPEN.format(url="j.url")}
        ORDER BY md5(j.url || %(seed)s) LIMIT %(n)s
        """,
        {"seed": seed, "n": n},
    )


def usage(res: Any) -> dict[str, int]:
    u = res.usage or {}
    return {
        "input_tokens": u.get("input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
    }


def deployed_verdicts(purpose: str, urls: list[str], params: dict[str, Any]) -> dict[str, dict]:
    """What production decided for these postings, in the step's own field
    shape, so an arm can be scored against the live answer as well as
    against the reference arm."""
    if purpose == "filter":
        row = db.query_one(
            "SELECT prompt_hash FROM user_filters WHERE id = %s", (params.get("filter_id"),)
        )
        if not row:
            return {}
        rows = db.query(
            """
            SELECT DISTINCT ON (url) url, status FROM ai_queries
            WHERE url = ANY(%s) AND check_type = 'custom' AND prompt_hash = %s
              AND status IN ('passed', 'rejected')
            ORDER BY url, id DESC
            """,
            (urls, row["prompt_hash"]),
        )
        return {r["url"]: {"should_filter": r["status"] == "rejected"} for r in rows}
    if purpose == "verify":
        rows = db.query(
            """
            SELECT DISTINCT ON (url, check_type) url, check_type, status FROM ai_queries
            WHERE url = ANY(%s) AND check_type IN ('closed', 'clearance')
              AND status IN ('passed', 'rejected')
            ORDER BY url, check_type, id DESC
            """,
            (urls,),
        )
        out: dict[str, dict] = {}
        for r in rows:
            key = (
                "is_closed" if r["check_type"] == "closed" else "requires_clearance_or_restrictions"
            )
            out.setdefault(r["url"], {})[key] = r["status"] == "rejected"
        return out
    if purpose == "comp":
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
    if purpose == "requirements":
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
    return {}


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


def summarise(experiment_id: int) -> dict[str, Any]:
    exp = db.query_one("SELECT purpose, params FROM ai_experiments WHERE id = %s", (experiment_id,))
    assert exp is not None
    step = steps()[exp["purpose"]]
    rows = db.query(
        "SELECT arm, url, output, usage, cost_usd, error FROM ai_experiment_results "
        "WHERE experiment_id = %s ORDER BY arm, url",
        (experiment_id,),
    )
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
        a["cost_usd"] += float(r["cost_usd"] or 0)
        if r["output"] is None:
            a["failed"] += 1
        else:
            a["ok"] += 1
            fields_by_arm.setdefault(r["arm"], {})[r["url"]] = step["fields"](r["output"])
    urls = sorted({r["url"] for r in rows})
    deployed = deployed_verdicts(exp["purpose"], urls, exp["params"])
    reference = exp["params"].get("reference") or (
        max(by_arm, key=lambda k: by_arm[k]["cost_usd"]) if by_arm else None
    )
    for arm, a in by_arm.items():
        n = a["n"] or 1
        a["cost_per_100_usd"] = round(a["cost_usd"] / n * 100, 4)
        a["input_per_request"] = round(a["input_tokens"] / n)
        a["output_per_request"] = round(a["output_tokens"] / n)
        a["reasoning_per_request"] = round(a["reasoning_tokens"] / n)
        a["cost_usd"] = round(a["cost_usd"], 4)
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
        if exp["purpose"] == "filter" and mine:
            a["pass_rate"] = round(
                sum(1 for f in mine.values() if not f.get("should_filter")) / len(mine), 3
            )
    skipped = exp["params"].get("skipped") or {}
    expected_arms = {
        arm_name(arm["model"], arm["effort"]) for arm in exp["params"].get("arms", [])
    } - set(skipped)
    sampled = exp["params"].get("sampled")
    expected = sampled * len(expected_arms) if sampled is not None else None
    return {
        "reference": reference,
        "arms": by_arm,
        "postings": len(urls),
        "expected_results": expected,
        "received_results": len(rows),
        "missing_results": max(0, expected - len(rows)) if expected is not None else None,
        "missing_arms": sorted(expected_arms - set(by_arm)),
    }
