from __future__ import annotations

import pytest

from core import pricing, providers, shapes
from core.routing import resolve
from tasks import HANDLERS
from tasks.mail.classification import handler as mail_classify
from tests.mail_classify_support import _store

# Probed against the live APIs. Each model REJECTS the other's cheapest value,
# so the intersection is only {low, medium, high}.
_PROBED_ACCEPTS = {
    "gpt-5-mini": {"minimal", "low", "medium", "high"},
    "gpt-5.6-luna": {"none", "low", "medium", "high", "xhigh", "max"},
    # Probed 2026-09-22 the same way, against the live API: sending "minimal"
    # returns "Unsupported value: 'minimal' is not supported with the
    # 'gpt-6-luna' model. Supported values are: 'none', 'low', 'medium',
    # 'high', 'xhigh', and 'max'." The same set as its predecessor, which is
    # what made the swap a one-line change.
    "gpt-6-luna": {"none", "low", "medium", "high", "xhigh", "max"},
}


def test_both_models_are_priced():
    """If a model is missing from the price table its spend books as NULL and
    the classification run is invisible to /admin/spend - a silent hole in
    exactly the surface built to catch silent holes."""
    assert pricing.rates_for(shapes.BACKFILL_MODEL) is not None
    assert pricing.rates_for(shapes.ONGOING_MODEL) is not None


def test_handler_is_registered():
    assert HANDLERS["classify_mail"] is mail_classify.handle_classify_mail


def test_both_mail_paths_read_the_mail_the_same_way():
    """One model for the archive and the trickle, on Kanishk's call, and the
    reason is consistency rather than the $4.55 difference. 67k messages were
    classified on luna; an ongoing feed on another model reads the same mail by
    different standards, so a rejection recognised in the archive might not be
    recognised next week - and that shows up as a change in the funnel that
    nothing in the funnel explains.

    The constants stay separate because their env overrides are separate: the
    per-task model config can move one path without the other."""
    assert shapes.BACKFILL_MODEL == shapes.ONGOING_MODEL

    # Still separate purposes, so the two remain separable in the spend ledger
    # even while they share a model.
    assert shapes.BACKFILL_TASK.purpose != shapes.ONGOING_TASK.purpose


def test_each_model_gets_an_effort_it_actually_accepts():
    """The bug this replaces: the original test asserted the effort was in
    ai._EFFORTS_OPENAI, a UNION across model generations. "none" is in that
    union because the 5.6 family accepts it, so the test passed while
    gpt-5-mini rejected the value on every call - 400, and a batch submits
    whole and fails whole.

    Backfill worked and ongoing did not, which is the worst shape for
    noticing: the path exercised by hand was fine and the scheduled one was
    dead. Validating against a union is what hid it, so this validates
    against the models actually configured.
    """
    for shape in (shapes.BACKFILL_TASK, shapes.ONGOING_TASK):
        model = resolve(shape).model
        accepts = _PROBED_ACCEPTS.get(model)
        assert accepts is not None, f"{model} configured but never probed"
        assert shape.resolved_effort() in accepts


def test_effort_follows_the_model_rather_than_a_shared_constant():
    """#179: a single CLASSIFY_EFFORT was shared across both paths, and mini
    rejects "none" outright - so the ongoing path 400'd on every call while the
    backfill worked. A batch submits whole and fails whole, so a rejected
    parameter costs the entire run.

    The two task shapes have separate model overrides, even though their
    defaults agree. Each shape derives its effort from its selected model's
    declared set, so changing one model also changes the effort it sends."""
    for shape in (shapes.BACKFILL_TASK, shapes.ONGOING_TASK):
        model = resolve(shape).model
        declared = providers.model(model)
        assert declared is not None
        assert shape.resolved_effort() not in declared.reasoning.rejects


def test_max_tokens_leaves_room_for_the_schema():
    """Too small truncates JSON mid-string, which arrives as an unparsable
    line rather than an error - so it looks like a model failure, not a
    configuration one."""
    assert shapes.CLASSIFY_MAX_TOKENS >= 200


def test_the_budget_prices_the_cap_the_handler_applies(set_config):
    """The handler capped a task at 1,200 while the fleet budget priced a
    cycle at 5,000, each from its own guess at a spec's size."""
    from api.task_config import configured_shape

    set_config("mail_classify_per_cycle", 7)
    for shape in (shapes.BACKFILL_TASK, shapes.ONGOING_TASK):
        assert configured_shape(shape).per_cycle == 7


@pytest.mark.asyncio
async def test_the_cap_is_clamped_not_trusted(set_config, monkeypatch, f):
    """An enqueuer asking for the whole mailbox would build a spec list far
    larger than a wave can carry, and that failure arrives as memory pressure
    on a worker rather than as a rejected parameter."""
    seen: dict = {}

    async def fake(task_id, shape, specs):
        from core.routing import resolve

        seen["count"] = len(specs)
        return [], resolve(shape)

    for i in range(3):
        _store(f, mid=f"<cap{i}@x>")
    monkeypatch.setattr(mail_classify, "run_batched", fake)
    monkeypatch.setattr(mail_classify, "set_progress", lambda *a, **k: None)
    set_config("mail_classify_per_cycle", 2)
    await mail_classify.handle_classify_mail(1, {"cap": 999999})
    assert seen["count"] <= 2
