"""Task handlers grouped by domain workflow.

HANDLERS is the only thing the worker loop needs from this package. Importing
them here keeps the loop from knowing which module any given kind lives in,
and keeps handlers from importing the loop.

What each task DECLARES - its purpose, its models, its per-cycle size - is not
here. That is core/shapes.py, so the services can price and configure a task
without importing the code that runs it.
"""

from __future__ import annotations

from tasks.application import handle_application_draft, handle_application_sweep
from tasks.batch_objects import handle_consolidate_batch_objects
from tasks.batches import handle_poll_batches
from tasks.board import handle_recompute_board
from tasks.content import handle_fetch_missing_content
from tasks.derivations.compensation import PAY
from tasks.derivations.embeddings import EMBEDDINGS
from tasks.derivations.job_profiles import PROFILES
from tasks.derivations.locations import LOCATIONS
from tasks.derivations.requirements import REQUIREMENTS
from tasks.derivations.sweep import Derivation
from tasks.digests import handle_send_digests
from tasks.filtering.personal_runs import (
    handle_run_all_filters,
    handle_run_filter,
    handle_run_filter_batch_chunk,
    handle_run_filter_chunk,
)
from tasks.health import handle_data_health
from tasks.ingest import handle_ingest_source, handle_retire_switched_off
from tasks.ledger_rollup import handle_ledger_rollup
from tasks.mail.classification.handler import handle_classify_mail
from tasks.mail.matching.handler import handle_match_mail
from tasks.mail.pointers import handle_backfill_mail_pointers
from tasks.mail.sync import (
    handle_import_archive,
    handle_probe_credentials,
    handle_sync_gmail,
)
from tasks.managed_boards import handle_run_managed_board, handle_run_managed_board_batch
from tasks.page_text_tiering import handle_tier_page_text
from tasks.resume_uploads import handle_process_resume_upload
from tasks.uploads import handle_extract_upload
from tasks.verify import (
    handle_reverify_chunk,
    handle_reverify_open,
    handle_verify_new,
)

# Every derived fact, switched on or off. The worker schedules each one and
# dispatches its kind to the shared sweep (tasks.derivations.sweep).
DERIVATIONS: tuple[Derivation, ...] = (PAY, REQUIREMENTS, PROFILES, EMBEDDINGS, LOCATIONS)

HANDLERS = {
    "extract_upload": lambda task_id, payload: handle_extract_upload(payload),
    "process_resume_upload": lambda task_id, payload: handle_process_resume_upload(payload),
    "classify_mail": handle_classify_mail,
    "match_mail": handle_match_mail,
    "sync_gmail": handle_sync_gmail,
    "import_archive": handle_import_archive,
    "probe_credentials": handle_probe_credentials,
    "run_filter": handle_run_filter,
    "run_all_filters": handle_run_all_filters,
    "run_filter_chunk": handle_run_filter_chunk,
    "run_filter_batch_chunk": handle_run_filter_batch_chunk,
    "ingest_source": handle_ingest_source,
    "retire_switched_off": handle_retire_switched_off,
    "tier_page_text": handle_tier_page_text,
    "reverify_open": handle_reverify_open,
    "reverify_chunk": handle_reverify_chunk,
    "recompute_board": handle_recompute_board,
    "send_digests": handle_send_digests,
    "data_health": handle_data_health,
    "ledger_rollup": handle_ledger_rollup,
    "poll_batches": handle_poll_batches,
    "verify_new": handle_verify_new,
    "fetch_missing_content": handle_fetch_missing_content,
    "application_draft": handle_application_draft,
    "application_sweep": handle_application_sweep,
    "run_managed_board": handle_run_managed_board,
    "run_managed_board_batch": handle_run_managed_board_batch,
    "backfill_mail_pointers": handle_backfill_mail_pointers,
    "consolidate_batch_objects": handle_consolidate_batch_objects,
    **{d.kind: d.handle for d in DERIVATIONS},
}

__all__ = ["DERIVATIONS", "HANDLERS"]
