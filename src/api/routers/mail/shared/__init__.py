"""The parts more than one of these surfaces needs, spelled once.

Two reasons a thing is here rather than beside its route. The administrator
and the owner do the same job over different mailboxes - correcting what a
message is, listing what it could belong to - and a second copy of either
would drift from the first. And evidence is attached wherever a match or a
proposal is shown, which is two surfaces that are otherwise unrelated.
"""

from api.routers.mail.shared.candidates import Candidates, candidates_payload
from api.routers.mail.shared.classification import (
    Reclassification,
    Reclassified,
    Reverted,
    apply_classification,
    apply_revert,
)
from api.routers.mail.shared.evidence import Evidence, evidence_for

__all__ = [
    "Candidates",
    "Evidence",
    "Reclassification",
    "Reclassified",
    "Reverted",
    "apply_classification",
    "apply_revert",
    "candidates_payload",
    "evidence_for",
]
