"""Which email addresses belong to the mailbox's owner.

Direction is a fact the headers already carry, but reading it needs to know
whose addresses are whose - and `users.email` holds one address while a person
has several. In this corpus it holds kanishksachdev@gmail.com while the same
mailbox also owns kqs6171@psu.edu, which is the sender of 1,840 messages that
the self-sent guard therefore never saw.

DERIVED FROM THE MAILBOX, NOT CONFIGURED. A hardcoded list is wrong for the
next user and stale for this one, and there is a property that settles it
without asking anyone: a mailbox's own addresses appear on nearly everything it
holds, and no single correspondent does. Measured here over 67,257 messages and
22,157 distinct addresses:

    kanishksachdev@gmail.com   34,022   50.6%
    kqs6171@psu.edu            22,846   34.0%
    notifications@github.com    5,322    7.9%   <- 4.29x drop, the largest in the list
    notifications@instructure    4,059    6.0%

Gmail's users.settings.sendAs.list would give the identities outright, but the
OAuth grant is gmail.readonly today, and an imported archive has no API at all.
So this stays the answer for archives regardless of what the scope becomes.
"""

from __future__ import annotations

from dataclasses import dataclass

# The floor exists ONLY to keep the gap search away from the tail. It does not
# separate owners from correspondents - that is the gap's job, and having both
# mechanisms cut is how the first version of this returned one address instead
# of two: a 10% floor left exactly the two owner addresses as candidates, and
# the gap rule then dutifully cut between them at their 1.49x ratio.
#
# Below about a percent of a mailbox, counts are small enough that integer
# quantization dominates: measured here, the largest ratio outside the top two
# is 2.00x, and it is an address on 2 messages beside one on 1. At 1% of this
# corpus - 672 messages - a ratio reflects the property being measured rather
# than rounding.
MIN_CANDIDATE_SHARE = 0.01

# Below this many messages a share is not measurable at the resolution the rule
# uses: one message moves a share by more than MIN_CANDIDATE_SHARE itself, so
# every address in the mailbox clears the floor and the sole correspondent in a
# one-message mailbox appears on 100% of it - which is what "the owner appears
# on nearly everything" looks like, with none of the meaning.
#
# 1 / MIN_CANDIDATE_SHARE, so the two move together and cannot disagree. Below
# it the answer is "we cannot tell", which the caller reads as "use the address
# you were configured with" rather than as "there are no identities".
MIN_MAILBOX_MESSAGES = int(1 / MIN_CANDIDATE_SHARE)

# No mailbox has an unbounded number of its own addresses, and a rule that
# could return dozens would silence a correspondent who happens to be busy.
# Belt and braces with the share floor: both would have to be wrong at once.
MAX_IDENTITIES = 5


@dataclass(frozen=True)
class AddressCount:
    address: str
    messages: int


def derive_identities(counts: list[AddressCount], total_messages: int) -> set[str]:
    """The owner's own addresses, from how much of the mailbox each appears on.

    Ranks candidates by share and cuts at the largest multiplicative gap. The
    gap is what makes this adaptive rather than a threshold in disguise: a
    mailbox with one address cuts after one, a mailbox with three cuts after
    three, and neither needs a different number.

    The cut only happens where the gap is the largest in the ranked list, so
    two addresses of similar size stay together - which is exactly what two
    addresses belonging to one person look like.

    Returns an empty set rather than guessing when the mailbox is too small to
    have the property - a handful of messages says nothing about who owns them,
    and an empty set means the caller falls back to the address it was told.
    That case is not hypothetical: a one-message mailbox makes its single
    correspondent appear on 100% of the mail, and an earlier version of this
    duly called a greenhouse.io no-reply address the owner.
    """
    if total_messages < MIN_MAILBOX_MESSAGES:
        return set()
    ranked = sorted(
        (c for c in counts if c.messages / total_messages >= MIN_CANDIDATE_SHARE),
        key=lambda c: (-c.messages, c.address),
    )[:MAX_IDENTITIES]
    if not ranked:
        return set()
    # The cut is after the largest ratio between neighbours. With one candidate
    # there is no gap to find and the single address is the answer.
    cut = len(ranked)
    best = 1.0
    for i in range(len(ranked) - 1):
        ratio = ranked[i].messages / max(ranked[i + 1].messages, 1)
        if ratio > best:
            best, cut = ratio, i + 1
    return {c.address.lower() for c in ranked[:cut]}
