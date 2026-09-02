"""Prompt identity: what was asked, recorded once rather than 71,725 times.

Every batched extraction records its tokens and its cost and nothing about the
instructions that produced them, so "what changed when this prompt changed" -
the question ai_queries answers for filters - cannot be asked of comp,
requirements or mail classification at all.

WHY THE TEXT AND NOT JUST A HASH. Measured on production: 68,735 rows of
ai_queries carry 21 distinct instruction texts between them - 3 for closed, 2
for clearance, 16 for custom - because instructions are module constants shared
by every request in a sweep. Stored per row that is 75 MB; stored once per
distinct text it is about 32 KB. A hash alone would answer "did this change"
and would cost about what the deduplicated text costs, so it would be strictly
less useful for no saving. That 2,300x redundancy is what lets this table be
honest about the text.

THIS FORKS NOTHING. ai_queries keys custom verdicts on
(url, check_type, prompt_hash), so changing a filter's prompt deliberately
makes every prior verdict unreachable. That is right for filters, where a
changed question means the old answer no longer applies, and wrong everywhere
else: making a comp or requirements prompt change invalidate 49k extracted rows
would re-pay for the whole catalog. Nothing here is a resolution key - these
records are read by people, not by the pipeline.
"""

from __future__ import annotations

import hashlib

# The probability of MISSING a change that affects a fraction p of outputs is
# (1-p)^n. At 100 samples a change touching 5% of rows is missed 0.6% of the
# time; one touching 1% is missed 37%. So 100 is the point where a prompt edit
# with any real effect is caught, and a subtle one is honestly not promised.
#
# It is also the scale the audits on this codebase have actually used: the
# gpt-5-nano fabrication finding came from 60 postings and the embedding recall
# curve from 400, and neither needed a census. The cap is per prompt VERSION,
# not per sweep, so a prompt running hourly for a year holds 100 rows, not
# 8,760.
PROMPT_SAMPLE_SIZE = 100


def prompt_hash(instructions: str) -> str:
    """Stable identity for a set of instructions.

    sha256 over the exact bytes sent, deliberately not normalised: the question
    is "is this the same text we sent last time", and a normaliser is one more
    thing that can disagree with itself between versions.
    """
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()
