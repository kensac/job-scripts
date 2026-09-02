"""What a posting embedding is: which model, how wide, and how much text.

Lives in core because the ORM, the sweep and the tests all need the same
numbers. The migration deliberately does NOT import from here - a migration
has to mean the same thing forever, and a constant that moves would silently
rewrite what an old migration did. It carries a frozen literal instead, and a
test pins the live column's width to EMBEDDING_DIMENSIONS so the two cannot
drift apart unnoticed.
"""

from __future__ import annotations

EMBEDDING_MODEL = "text-embedding-3-small"


# The model's native width, kept rather than reduced. text-embedding-3 vectors
# are Matryoshka, so a shorter vector is a truncation-and-renormalisation of
# this one and the choice is measurable rather than a matter of taste. Over 400
# real postings, scored against the full-width neighbour set:
#
#     1536 dims   127 MB   100%   (reference)
#      768 dims    64 MB    87%
#      512 dims    43 MB    82%
#      256 dims    21 MB    70%
#
# recall@5 and recall@10 agreed to within half a point, so that is the shape of
# the curve and not an artifact of k. Truncating to 512 would change nearly one
# in five of the roles a "more like this" panel shows, to save 85 MB on a
# database that has just shed 311 MB of orphaned backup tables. At ten million
# rows the trade inverts; at 20,730 it does not.
EMBEDDING_DIMENSIONS = 1536


# The same window the requirements extraction reads, and for the same reason:
# past it a page is boilerplate - similar-role lists, cookie notices, EEO text -
# which is exactly the content that would make two unrelated postings on the
# same applicant-tracking system look alike.
EMBEDDING_INPUT_CHARS = 20000


# Inputs per embeddings request. The API takes an array, so this is the
# difference between 20,730 round trips and 208. The provider caps a single
# request at 300,000 tokens; at the corpus mean of 1,132 tokens a posting that
# ceiling is 265, and 100 leaves room for the long tail without splitting a
# request server-side.
EMBEDDING_BATCH_SIZE = 100
