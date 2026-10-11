"""Persisted applications settings and their admin metadata."""

from __future__ import annotations

from pydantic import PositiveInt

from api.config_types import ConfigKey

CONFIG_KEYS: dict[str, ConfigKey] = {
    "resume_legacy_clear_enabled": ConfigKey(
        section="Applications",
        default=False,
        value_type=bool,
        help="Clear verified old resume values on Hetzner only after every old text and "
        "PDF has a canonical pointer and all readers use the new storage.",
    ),
    "resume_backfill_enabled": ConfigKey(
        section="Applications",
        default=False,
        value_type=bool,
        help="Copy old resume text to verified parts on Hetzner after old content writers "
        "are retired. PDF copying also requires resume_pdf_uploads_enabled and a bucket "
        "checked for expiry rules and capacity.",
    ),
    "resume_pdf_uploads_enabled": ConfigKey(
        section="Applications",
        default=False,
        value_type=bool,
        help="Accept new PDF resume uploads only after the object bucket has been checked "
        "for lifecycle expiry and enough capacity. Existing resumes remain readable while off.",
    ),
    # The hourly application sweep, per person: how many unread forms it
    # reads (one request each to the ATS, under the host budget; 1,307
    # readable postings were on the board on 2026-09-06, so the first pass
    # takes a working day at this rate and the ATSs see a trickle) and how
    # many missing drafts it batches (about $0.0005 each on luna).
    "application_form_reads_per_cycle": ConfigKey(
        section="Applications",
        default=150,
        value_type=PositiveInt,
        help="Application forms the hourly sweep reads per person per cycle, newest postings "
        "first, one request each to the ATS under the host budget.",
    ),
    # Empty means the built-in text in the code; see the admin registry.
    "application_draft_instructions": ConfigKey(
        section="Applications",
        kind="text",
        default="",
        value_type=str,
        help="The rules the model drafts application answers under, before the person's own "
        "writing style. Empty means the built-in text in api.apply.drafting; a change "
        "here takes effect on the next draft, with no roll. Read by application_draft, "
        "application_sweep and the refine endpoint. Shared evidence and company-motivation "
        "guidance always applies after these rules and the writing style.",
    ),
    "application_suggest_instructions": ConfigKey(
        section="Applications",
        kind="text",
        default="",
        value_type=str,
        help="The rules the model fills the rest of an application form under (the fields the "
        "profile and drafts did not). Empty means the built-in text in api.routers.apply. "
        "Read by POST /user/apply/suggest. The person's writing style and shared evidence "
        "and company-motivation guidance are appended to these rules.",
    ),
    # The model never fills these on a form; the person does. One label a
    # line or comma-separated, whole words in the field's label or key.
    "application_ai_never_fills": ConfigKey(
        section="Applications",
        default="location",
        value_type=str,
        help="Fields the model never fills on an application form, left to the person: one "
        "label a line or comma-separated, matched as whole words in the field's label or "
        "key. Read by POST /user/apply/suggest, which drops them before the call and names "
        "them in its reply so the extension lists them as the person's. Empty is not off: "
        "an empty list lets the model fill every field, the location box included.",
    ),
    "application_drafts_per_cycle": ConfigKey(
        section="Applications",
        default=500,
        value_type=PositiveInt,
        help="Missing application answers the hourly sweep drafts per person per cycle, in one "
        "half-price batch; about $0.0005 each on the sanctioned model.",
    ),
}
