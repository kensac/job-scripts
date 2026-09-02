"""Task handlers, one module per family.

HANDLERS is the only thing the worker loop needs from this package. Importing
them here keeps the loop from knowing which module any given kind lives in,
and keeps handlers from importing the loop.
"""

from __future__ import annotations

from api.tasks.batches import handle_poll_batches
from api.tasks.comp import handle_extract_comp
from api.tasks.content import handle_fetch_missing_content
from api.tasks.digests import handle_send_digests
from api.tasks.filters import (
    handle_run_all_filters,
    handle_run_filter,
    handle_run_filter_batch_chunk,
    handle_run_filter_chunk,
)
from api.tasks.health import handle_data_health
from api.tasks.ingest import handle_ingest_source
from api.tasks.mail_classify import handle_classify_mail
from api.tasks.requirements import handle_extract_requirements
from api.tasks.uploads import handle_extract_upload
from api.tasks.verify import (
    handle_reverify_chunk,
    handle_reverify_open,
    handle_verify_new,
)

HANDLERS = {
    "extract_upload": lambda task_id, payload: handle_extract_upload(payload),
    "classify_mail": handle_classify_mail,
    "run_filter": handle_run_filter,
    "run_all_filters": handle_run_all_filters,
    "run_filter_chunk": handle_run_filter_chunk,
    "run_filter_batch_chunk": handle_run_filter_batch_chunk,
    "ingest_source": handle_ingest_source,
    "reverify_open": handle_reverify_open,
    "reverify_chunk": handle_reverify_chunk,
    "extract_comp": handle_extract_comp,
    "extract_requirements": handle_extract_requirements,
    "send_digests": handle_send_digests,
    "data_health": handle_data_health,
    "poll_batches": handle_poll_batches,
    "verify_new": handle_verify_new,
    "fetch_missing_content": handle_fetch_missing_content,
}

__all__ = ["HANDLERS"]
