"""Derive which email addresses belong to a mailbox owner from address frequency.

`users.email` holds only one address, Gmail OAuth is read-only (no sendAs API),
and imported archives have no API. An owner's addresses appear on a far larger
share of the mailbox than any single correspondent.
"""

from __future__ import annotations

from dataclasses import dataclass

# Floor to keep the gap search out of the low-count tail where integer rounding
# creates noisy ratios. Must stay low enough that correspondents remain below
# the owner cluster for the gap cut to separate them.
MIN_CANDIDATE_SHARE = 0.01

# Coupled to MIN_CANDIDATE_SHARE so a single message cannot clear the floor on
# its own; below this count derive_identities returns empty so callers fall back
# to the configured address.
MIN_MAILBOX_MESSAGES = int(1 / MIN_CANDIDATE_SHARE)

# Upper bound on derived owner addresses so busy correspondents cannot be swept in.
MAX_IDENTITIES = 5


@dataclass(frozen=True)
class AddressCount:
    address: str
    messages: int


def derive_identities(counts: list[AddressCount], total_messages: int) -> set[str]:
    """Return the owner's email addresses by ranking candidates and cutting at the
    largest multiplicative drop between adjacent shares.

    Returns an empty set when the mailbox has fewer than MIN_MAILBOX_MESSAGES so
    the caller falls back to the configured address rather than guessing.
    """
    if total_messages < MIN_MAILBOX_MESSAGES:
        return set()
    ranked = sorted(
        (c for c in counts if c.messages / total_messages >= MIN_CANDIDATE_SHARE),
        key=lambda c: (-c.messages, c.address),
    )[:MAX_IDENTITIES]
    if not ranked:
        return set()
    # Cut after the largest ratio between neighbors; a single candidate stays intact.
    cut = len(ranked)
    best = 1.0
    for i in range(len(ranked) - 1):
        ratio = ranked[i].messages / max(ranked[i + 1].messages, 1)
        if ratio > best:
            best, cut = ratio, i + 1
    return {c.address.lower() for c in ranked[:cut]}
