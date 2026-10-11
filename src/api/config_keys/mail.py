"""Persisted mail settings and their admin metadata."""

from __future__ import annotations

from pydantic import PositiveInt

from api.config_types import ConfigKey
from core.shapes import (
    CLASSIFY_PER_CYCLE,
)

CONFIG_KEYS: dict[str, ConfigKey] = {
    "mail_classification_enabled": ConfigKey(
        section="Mail",
        default=True,
        value_type=bool,
        help="Whether mailbox syncs, archive imports, and the hourly scheduler enqueue new "
        "mail classification work. Turning it off does not discard already submitted batches.",
    ),
    "mail_classify_per_cycle": ConfigKey(
        section="Mail",
        default=CLASSIFY_PER_CYCLE,
        value_type=PositiveInt,
        help="Messages one mail classification task sends to the model, for the hourly "
        "sweep and an archive backfill alike. The default fills every batch wave that runs "
        "at once; more only waits in the provider's queue. The fleet budget prices a cycle "
        "at this many.",
    ),
    # Gmail's own page size is 500 and a per-message get is one quota unit,
    # so this bounds one task rather than the provider: a sync that cannot
    # finish resumes next cycle from the messages not yet stored.
    "mail_sync_per_cycle": ConfigKey(
        section="Mail",
        default=500,
        value_type=PositiveInt,
        help="New messages one Gmail sync stores per mailbox. The rest wait for the next cycle.",
    ),
}
