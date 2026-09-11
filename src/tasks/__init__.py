"""Task handlers, one module per family.

HANDLERS is the only thing the worker loop needs from this package. Importing
them here keeps the loop from knowing which module any given kind lives in,
and keeps handlers from importing the loop.

What each task DECLARES - its purpose, its models, its per-cycle size - is not
here. That is core/shapes.py, so the services can price and configure a task
without importing the code that runs it.
"""

from __future__ import annotations

from tasks.application import handle_application_draft, handle_application_sweep
from tasks.batches import handle_poll_batches
from tasks.board import handle_recompute_board
from tasks.comp import handle_extract_comp
from tasks.content import handle_fetch_missing_content
from tasks.digests import handle_send_digests
from tasks.embeddings import handle_embed_postings, handle_embed_postings_batch
from tasks.experiments import handle_run_experiment
from tasks.filters import (
    handle_run_all_filters,
    handle_run_filter,
    handle_run_filter_batch_chunk,
    handle_run_filter_chunk,
)
from tasks.health import handle_data_health
from tasks.ingest import handle_ingest_source
from tasks.locations import handle_classify_locations
from tasks.mail_classify import handle_classify_mail
from tasks.mail_match import handle_match_mail
from tasks.mail_sync import (
    handle_import_archive,
    handle_probe_credentials,
    handle_sync_gmail,
)
from tasks.message_html import handle_backfill_message_html
from tasks.requirements import handle_extract_requirements
from tasks.uploads import handle_extract_upload
from tasks.user_job_backfill import handle_backfill_user_job_split
from tasks.verify import (
    handle_reverify_chunk,
    handle_reverify_open,
    handle_verify_new,
)

HANDLERS = {
    "extract_upload": lambda task_id, payload: handle_extract_upload(payload),
    "classify_mail": handle_classify_mail,
    "backfill_message_html": handle_backfill_message_html,
    "match_mail": handle_match_mail,
    "sync_gmail": handle_sync_gmail,
    "import_archive": handle_import_archive,
    "probe_credentials": handle_probe_credentials,
    "run_filter": handle_run_filter,
    "run_all_filters": handle_run_all_filters,
    "run_filter_chunk": handle_run_filter_chunk,
    "run_filter_batch_chunk": handle_run_filter_batch_chunk,
    "ingest_source": handle_ingest_source,
    "reverify_open": handle_reverify_open,
    "reverify_chunk": handle_reverify_chunk,
    "extract_comp": handle_extract_comp,
    "extract_requirements": handle_extract_requirements,
    "classify_locations": handle_classify_locations,
    "recompute_board": handle_recompute_board,
    "embed_postings": handle_embed_postings,
    "embed_postings_batch": handle_embed_postings_batch,
    "send_digests": handle_send_digests,
    "data_health": handle_data_health,
    "poll_batches": handle_poll_batches,
    "verify_new": handle_verify_new,
    "fetch_missing_content": handle_fetch_missing_content,
    "application_draft": handle_application_draft,
    "application_sweep": handle_application_sweep,
    "run_experiment": handle_run_experiment,
    "backfill_user_job_split": handle_backfill_user_job_split,
}

__all__ = ["HANDLERS"]
