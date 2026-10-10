"""Task handlers, one module per family.

HANDLERS is the only thing the worker loop needs from this package. Importing
them here keeps the loop from knowing which module any given kind lives in,
and keeps handlers from importing the loop.

What each task DECLARES - its purpose, its models, its per-cycle size - is not
here. That is core/shapes.py, so the services can price and configure a task
without importing the code that runs it.
"""

from __future__ import annotations

from tasks.answer_links import handle_link_answers
from tasks.application import handle_application_draft, handle_application_sweep
from tasks.batches import handle_poll_batches
from tasks.board import handle_recompute_board
from tasks.comp import PAY
from tasks.comp_clear import handle_clear_jobs_pay
from tasks.content import handle_fetch_missing_content
from tasks.derive import Derivation
from tasks.digests import handle_send_digests
from tasks.embeddings import EMBEDDINGS
from tasks.filters import (
    handle_run_all_filters,
    handle_run_filter,
    handle_run_filter_batch_chunk,
    handle_run_filter_chunk,
)
from tasks.health import handle_data_health
from tasks.ingest import handle_ingest_source, handle_retire_switched_off
from tasks.job_profiles import PROFILES
from tasks.locations import LOCATIONS
from tasks.mail_classify import handle_classify_mail
from tasks.mail_match import handle_match_mail
from tasks.mail_olm_twins import handle_merge_olm_twins
from tasks.mail_pointers import handle_backfill_mail_pointers
from tasks.mail_sync import (
    handle_import_archive,
    handle_probe_credentials,
    handle_sync_gmail,
)
from tasks.managed_boards import handle_run_managed_board, handle_run_managed_board_batch
from tasks.posting_uploads import handle_clear_upload_columns
from tasks.requirements import REQUIREMENTS
from tasks.uploads import handle_extract_upload
from tasks.verify import (
    handle_reverify_chunk,
    handle_reverify_open,
    handle_verify_new,
)

# Every derived fact, switched on or off. The worker schedules each one and
# dispatches its kind to the shared sweep (tasks.derive).
DERIVATIONS: tuple[Derivation, ...] = (PAY, REQUIREMENTS, PROFILES, EMBEDDINGS, LOCATIONS)

HANDLERS = {
    "extract_upload": lambda task_id, payload: handle_extract_upload(payload),
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
    "reverify_open": handle_reverify_open,
    "reverify_chunk": handle_reverify_chunk,
    "recompute_board": handle_recompute_board,
    "send_digests": handle_send_digests,
    "data_health": handle_data_health,
    "poll_batches": handle_poll_batches,
    "verify_new": handle_verify_new,
    "fetch_missing_content": handle_fetch_missing_content,
    "application_draft": handle_application_draft,
    "application_sweep": handle_application_sweep,
    "run_managed_board": handle_run_managed_board,
    "run_managed_board_batch": handle_run_managed_board_batch,
    "backfill_mail_pointers": handle_backfill_mail_pointers,
    "merge_olm_twins": handle_merge_olm_twins,
    "link_answers": handle_link_answers,
    "clear_jobs_pay": handle_clear_jobs_pay,
    "clear_upload_columns": handle_clear_upload_columns,
    **{d.kind: d.handle for d in DERIVATIONS},
}

__all__ = ["DERIVATIONS", "HANDLERS"]
