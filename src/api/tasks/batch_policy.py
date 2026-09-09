"""Scheduled work requires an implemented batch transport for its credentials."""

from api import ai, db
from api.tasks.runtime import set_progress
from core import providers


def scheduled(payload: dict) -> bool:
    if payload.get("scheduled") or payload.get("batched"):
        return True
    parent_id = payload.get("parent_id")
    if parent_id:
        parent = db.query_one("SELECT payload FROM tasks WHERE id = %s", (parent_id,))
        if parent:
            return bool(parent["payload"].get("scheduled") or parent["payload"].get("batched"))
    return False


def _unsupported(task_id: int, cfg: ai.AIConfig) -> None:
    message = (
        f"BATCH_UNSUPPORTED: scheduled work is blocked for {cfg.provider}/{cfg.model} "
        f"with {cfg.key_source} credentials; no supported batch transport"
    )
    set_progress(task_id, 0, 0, message)
    raise RuntimeError(message)


def require_transport(task_id: int, cfg: ai.AIConfig) -> None:
    # The persisted batch collector currently uses only the server OpenAI key.
    # A provider offering batches does not make a different credential usable
    # by that collector, and falling back would violate the scheduled intent.
    if (
        cfg.key_source != "owner"
        or cfg.provider != "openai"
        or providers.PROVIDERS["openai"].batch_endpoint is None
    ):
        _unsupported(task_id, cfg)


def transport(task_id: int, cfg: ai.AIConfig) -> str:
    """Which transport a scheduled run takes: "batch" on the shared key, or
    "live" on a person's own key.

    A person who brought their own key was promised no cap and their own
    bill (Kanishk, 2026-09-08), and the persisted batch collector only holds
    the server's OpenAI key, so their hourly sweep runs live, billed to them,
    rather than not at all. Refusing with BATCH_UNSUPPORTED is reserved for
    the shared key, where a live fallback would spend the shared budget at
    full price against the scheduled intent."""
    if cfg.key_source != "owner":
        return "live"
    require_config(task_id, cfg)
    return "batch"


def require_config(task_id: int, cfg: ai.AIConfig) -> None:
    require_transport(task_id, cfg)
    declared = providers.model(cfg.model)
    if (
        declared is None
        or providers.provider_of(cfg.model) != "openai"
        or declared.structured_output.mode is not providers.StructuredOutput.JSON_SCHEMA
    ):
        _unsupported(task_id, cfg)
