"""Which providers are out of credit, and where their work goes instead.

A provider's exhaustion is an open `health_alerts` row of kind
`provider_exhausted`, subject the provider. The alert is the state: every
worker reads the same row, the existing notifier mails it, and the row records
the reason and when it started and was last seen. It is not owned by a
detector, so a detection run never resolves it; a submission the provider
accepts does. An admin resolving it means "try the provider again".

Routing skips an exhausted provider for one scheduler cycle after its last
refusal (`ingest_interval_minutes`), then tries it again: a refused
submission costs nothing, and without the retry nothing would ever be sent to
observe the account paid again. Work goes to the purpose's fallback chain
(app_config model_fallback_chains, core.fallback) only when one is set.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from api import db, metrics, telemetry
from api.verdict_equivalence import answering_models
from core import providers, routing
from core.fallback import FILTER_PURPOSE, MANAGED_BOARD_PURPOSE, FallbackChains, next_choice
from core.routing import Choice, TaskShape, resolve

logger = logging.getLogger(__name__)

ALERT_KIND = "provider_exhausted"

# The verdict equivalence kind each verdict-writing purpose's readers go
# through (api.verdict_equivalence.answering_models). Such a purpose falls back
# only to models grouped with its primary: any other model's verdicts would be
# paid for and never read, by boards, filters or reuse checks. A purpose not
# listed writes rows that are not verdicts (extractions, classifications),
# which mix by model by design, so any chain entry may run it.
_VERDICT_KIND = {FILTER_PURPOSE: "custom", MANAGED_BOARD_PURPOSE: "custom", "verify": "verify"}


def chains() -> dict[str, list[str]]:
    return FallbackChains.model_validate(db.get_config("model_fallback_chains")).root


def chain(purpose: str) -> list[str]:
    return chains().get(purpose, [])


def equivalent(purpose: str, primary: str) -> list[str] | None:
    """Models whose verdicts count as `primary`'s for this purpose, or None
    when the purpose writes no verdicts."""
    kind = _VERDICT_KIND.get(purpose)
    return answering_models(kind, primary) if kind else None


def usable(purpose: str, primary: str, models: list[str]) -> list[str]:
    """The chain entries this purpose may fall back to from `primary`, in order."""
    group = equivalent(purpose, primary)
    return models if group is None else [m for m in models if m in group]


def exhausted() -> list[dict[str, Any]]:
    """Every provider currently marked out of credit, and whether routing is
    skipping it right now."""
    return db.query(
        """
        SELECT subject AS provider, message, detail->>'reason' AS reason,
               first_seen, last_seen,
               last_seen > now() - make_interval(mins => %(cycle)s) AS skipped
        FROM health_alerts
        WHERE kind = %(kind)s AND resolved_at IS NULL
        ORDER BY subject
        """,
        {"kind": ALERT_KIND, "cycle": int(db.get_config("ingest_interval_minutes"))},
    )


def blocked_providers() -> set[str]:
    return {row["provider"] for row in exhausted() if row["skipped"]}


def mark(provider: str, reason: str, *, model: str | None, purpose: str | None) -> None:
    """Record that `provider` refused work because the account cannot pay."""
    metrics.PROVIDER_EXHAUSTED.labels(provider).inc()
    logger.warning(f"{provider} is out of credit ({model}, {purpose}): {reason}")
    row = db.query_one(
        """
        INSERT INTO health_alerts (kind, subject, severity, message, detail)
        VALUES (%(kind)s, %(provider)s, 'critical', %(message)s, %(detail)s)
        ON CONFLICT (kind, subject) WHERE resolved_at IS NULL
        DO UPDATE SET last_seen = now()
        RETURNING id, (xmax = 0) AS is_new
        """,
        {
            "kind": ALERT_KIND,
            "provider": provider,
            "message": (
                f"{provider} refused work because the account cannot pay: {reason}. "
                "Purposes with a fallback chain (model_fallback_chains) run on its next "
                "model; the rest fail until the account is paid. The provider is tried "
                "again every scheduler cycle."
            ),
            "detail": db.jsonb({"reason": reason, "model": model, "purpose": purpose}),
        },
    )
    if row and row["is_new"]:
        telemetry.capture(
            "alert_opened",
            properties={
                "kind": ALERT_KIND,
                "subject": provider,
                "severity": "critical",
                "alert_id": row["id"],
            },
        )


def clear(provider: str) -> None:
    """The provider accepted new work, so its account can pay again."""
    rows = db.query(
        "UPDATE health_alerts SET resolved_at = now() "
        "WHERE kind = %s AND subject = %s AND resolved_at IS NULL RETURNING id",
        (ALERT_KIND, provider),
    )
    for row in rows:
        logger.info(f"{provider} accepted work again; its exhaustion alert is resolved")
        telemetry.capture(
            "alert_resolved",
            properties={"kind": ALERT_KIND, "subject": provider, "alert_id": row["id"]},
        )


def next_for(shape: TaskShape, primary: str, tried: set[str], blocked: set[str]) -> Choice | None:
    """The first chain entry that can run this shape, skipping `blocked`
    providers, `tried` models and, for a verdict purpose, models outside the
    primary's equivalence group; None when there is none."""
    models = usable(shape.purpose, primary, chain(shape.purpose))
    if not models:
        return None
    # A provider this host holds no key for cannot take the work either.
    unkeyed = {name for name in providers.PROVIDERS if not routing.server_key(name)}
    choice = next_choice(shape, models, blocked | unkeyed, tried)
    if not choice:
        return None
    provider = providers.provider_of(primary)
    return replace(
        choice,
        fallback_from=primary,
        reason=f"{provider} is out of credit; fallback from {primary}",
    )


def route(shape: TaskShape, override: str | None = None) -> Choice:
    """resolve(), then the fallback chain when the chosen provider is out of credit.

    With no chain, or no entry that can run now, the routed model is returned
    unchanged: it is tried, refused, and the task fails as it did before.
    """
    chosen = resolve(shape, override=override)
    blocked = blocked_providers()
    if chosen.provider not in blocked:
        return chosen
    return next_for(shape, chosen.model, {chosen.model}, blocked) or chosen


def record(purpose: str, primary: str, model: str, requests: int) -> None:
    """Count and report requests sent on a fallback. The batch rows hold the
    durable record (ai_batches.fallback_from)."""
    metrics.MODEL_FALLBACK_REQUESTS.labels(purpose, primary, model).inc(requests)
    logger.warning(f"{purpose}: {requests} request(s) on {model}, fallback from {primary}")
    telemetry.capture(
        "model_fallback",
        properties={
            "purpose": purpose,
            "from_model": primary,
            "to_model": model,
            "requests": requests,
        },
    )
