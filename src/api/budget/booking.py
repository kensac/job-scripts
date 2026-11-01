from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from api import model_calls


@dataclass(frozen=True)
class Booking:
    """Who a call is charged to, on whose key, and for what."""

    payer: model_calls.Payer
    key_source: str
    purpose: str


def book_live(
    booking: Booking,
    model: str | None,
    usage: Mapping[str, int | None],
    duration_ms: int | None = None,
) -> int | None:
    """Write a live call to the ledger and count its tokens; its id, for the
    answer it produced to point at, or None when nothing was billed."""
    return _book(booking, model, usage, batched=False, duration_ms=duration_ms)


def record_tokens(
    user_id: int,
    key_source: str,
    purpose: str,
    model: str | None,
    usage: Mapping[str, int | None],
    *,
    batched: bool = False,
) -> None:
    """A person's call. A live one is written to the call ledger here; a
    batched one is already there, written with its receipt by the checkpoint,
    so its consumer's booking only counts its tokens."""
    booking = Booking(model_calls.Payer(user_id=user_id), key_source, purpose)
    _book(booking, model, usage, batched=batched)


def record_managed_board_tokens(
    managed_board_id: int,
    purpose: str,
    model: str | None,
    usage: Mapping[str, int | None],
    *,
    batched: bool = False,
) -> None:
    """Server-key work owned by a managed board, never a fake user."""
    booking = Booking(model_calls.Payer(managed_board_id=managed_board_id), "owner", purpose)
    _book(booking, model, usage, batched=batched)


@contextmanager
def record_parse_failures(user_id: int, key_source: str, purpose: str, model: str | None):
    from api.ai import PaidParseError

    try:
        yield
    except PaidParseError as exc:
        record_tokens(user_id, key_source, purpose, model, exc.usage)
        raise


def _book(
    booking: Booking,
    model: str | None,
    usage: Mapping[str, int | None],
    *,
    batched: bool,
    duration_ms: int | None = None,
) -> int | None:
    total = usage.get("total_tokens")
    if not total:
        return None
    call_id = None
    if not batched:
        call = model_calls.Call(
            booking.purpose,
            model,
            booking.payer,
            booking.key_source,
            usage,
            duration_ms=duration_ms,
        )
        call_id = model_calls.record_live(call)
    from api import metrics

    metrics.AI_TOKENS.labels(booking.key_source, booking.purpose).inc(total)
    return call_id
