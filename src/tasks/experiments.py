"""Task orchestration for experiment runs."""

from __future__ import annotations

import datetime
import json
from decimal import Decimal
from typing import Any

from api import db
from api import experiments as domain
from api.ai.batch_results import progress_counts
from core import pricing
from tasks.runtime import (
    AwaitingBatch,
    batch_event_hook,
    collect_pending,
    consume_result,
    has_batch_work,
    park_awaiting_batch,
    pending_batch_ids,
    set_progress,
    snapshot_specs,
)


async def handle_run_experiment(task_id: int, payload: dict[str, Any]) -> None:
    """Submit one batch per arm, park, and on resume score every answer.
    Safe to run again from the top: a resumed task collects and scores.

    A failure lands on the experiment row as well as the task: the first
    requirements run failed in scoring and the experiment sat "running"
    with every answer in, because only the task knew."""
    try:
        await _run(task_id, payload)
    except AwaitingBatch:
        raise
    except Exception as exc:
        db.execute(
            "UPDATE ai_experiments SET status = 'failed', error = %s, finished_at = now() "
            "WHERE id = %s AND status <> 'done'",
            (str(exc)[:500], payload.get("experiment_id")),
        )
        raise


async def _run(task_id: int, payload: dict[str, Any]) -> None:
    from core.batch import structured_response_spec, submit_responses_batches

    experiment_id = payload["experiment_id"]
    experiment = db.query_one("SELECT * FROM ai_experiments WHERE id = %s", (experiment_id,))
    if not experiment:
        raise LookupError("unknown experiment")
    params = experiment["params"]
    step = domain.steps().get(experiment["purpose"])
    if step is None:
        raise LookupError(f"no measurable step named {experiment['purpose']}")
    db.execute(
        "UPDATE ai_experiments SET status = 'running', task_id = %s WHERE id = %s",
        (task_id, experiment_id),
    )
    if not has_batch_work(task_id):
        rows = domain.sample(
            int(params.get("sample") or 100), str(params.get("seed") or experiment_id)
        )
        if not rows:
            raise RuntimeError("no eligible postings to sample")
        db.execute(
            "UPDATE ai_experiments SET params = params || %s::jsonb WHERE id = %s",
            (json.dumps({"sampled": len(rows)}), experiment_id),
        )
        instructions = step.instructions(params)
        ids: list[str] = []
        skipped: dict[str, str] = {}
        for arm in params["arms"]:
            model, effort = arm["model"], arm["effort"]
            why = domain.arm_ok(model, effort)
            if why:
                skipped[domain.arm_name(model, effort)] = why
                continue
            specs = [
                structured_response_spec(
                    f"{domain.arm_name(model, effort)}|{r['url']}",
                    instructions,
                    step.build_input(r),
                    step.answer_model,
                    context={
                        "experiment_id": experiment_id,
                        "purpose": experiment["purpose"],
                        "arm": domain.arm_name(model, effort),
                        "url": r["url"],
                    },
                )
                for r in rows
            ]
            ids += await submit_responses_batches(
                snapshot_specs(task_id, specs),
                model,
                effort,
                step.max_output_tokens,
                on_event=batch_event_hook(task_id, domain.PURPOSE, model),
            )
        db.execute(
            "UPDATE ai_experiments SET params = params || %s::jsonb WHERE id = %s",
            (json.dumps({"skipped": skipped, "sampled": len(rows)}), experiment_id),
        )
        if not ids:
            db.execute(
                "UPDATE ai_experiments SET status = 'failed', error = 'no arm could run', "
                "finished_at = now() WHERE id = %s",
                (experiment_id,),
            )
            raise RuntimeError("no arm could run: " + json.dumps(skipped))
        set_progress(
            task_id, 0, len(rows) * (len(params["arms"]) - len(skipped)), "batches submitted"
        )
        if not park_awaiting_batch(task_id, ids):
            raise RuntimeError(
                f"submitted {len(ids)} batch(es) but task {task_id} was no longer claimable"
            )
        raise AwaitingBatch()

    results = await collect_pending(task_id, batch_event_hook(task_id, domain.PURPOSE, None))
    for result in results:
        with consume_result(task_id, result) as receipt:
            if not receipt.pending:
                continue
            context = result.request.context if result.request else None
            if not context:
                receipt.outcome = "unknown_request"
                continue
            if context.get("experiment_id") != experiment_id:
                receipt.outcome = "subject_mismatch"
                continue
            original_step = domain.steps().get(context["purpose"])
            if original_step is None:
                receipt.outcome = "unknown_request"
                continue
            usage = domain.usage(result)
            cost = pricing.estimate_cost_usd(
                result.model, usage["input_tokens"], usage["output_tokens"], batched=True
            )
            output = None
            error = result.error
            if result.text and not result.error:
                try:
                    output = original_step.answer_model.model_validate_json(result.text).model_dump(
                        mode="json"
                    )
                except ValueError as exc:
                    error = f"unparsable: {str(exc)[:200]}"
            db.execute(
                """
                INSERT INTO ai_experiment_results (experiment_id, arm, url, output, usage, cost_usd, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (experiment_id, arm, url) DO UPDATE
                    SET output = EXCLUDED.output, usage = EXCLUDED.usage,
                        cost_usd = EXCLUDED.cost_usd, error = EXCLUDED.error
                """,
                (
                    experiment_id,
                    context["arm"],
                    context["url"],
                    db.jsonb(output) if output is not None else None,
                    db.jsonb(usage),
                    Decimal(cost) if cost is not None else None,
                    error,
                ),
            )
            receipt.outcome = "written" if output is not None else "failed"
    stored, total = progress_counts(task_id, successful=("written", "failed"))
    if pending_batch_ids(task_id):
        # Stragglers still out: the worker parks again; the summary waits.
        set_progress(task_id, stored, total, "collected some batches, waiting on the rest")
        return
    summary = domain.summarise(experiment_id)
    db.execute(
        "UPDATE ai_experiments SET status = 'done', summary = %s, finished_at = %s WHERE id = %s",
        (db.jsonb(summary), datetime.datetime.now(datetime.UTC), experiment_id),
    )
    set_progress(task_id, stored, total, "scored")
