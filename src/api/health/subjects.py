"""What an alert's `subject` holds, per detector."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# What an alert's `subject` actually holds, per detector.
#
# It is not one kind of thing: two detectors put a source in it, one a host,
# one a provider and user id, one a task kind. The dashboard linked all of them
# to /job-scripts/sources?src=<subject>, which is right for two of five - the
# other three sent an operator to a page that selects nothing, and an empty
# sources page reads as a source that has disappeared rather than as a link
# that was never right.
#
# Declared here rather than guessed from the value client-side, which would be
# the frontend encoding semantics it cannot see.
#
# Kept as a map from alert kind rather than a column on health_alerts, because
# it is a property of the DETECTOR and never varies between two alerts of the
# same kind. A column would store the same answer on every row, go stale if a
# detector changed what it puts in subject, and need a backfill to say anything
# about the alerts already open.
SUBJECT_SOURCE = "source"
SUBJECT_HOST = "host"
SUBJECT_PROVIDER_USER = "provider_user"
SUBJECT_TASK = "task"
# The spend ledger's grouping key, not a task kind: the batch purpose is
# "mail_classify" where the task kind is "classify_mail". Linking one to the
# other lands on nothing.
SUBJECT_PURPOSE = "purpose"
# A fleet worker's name (tasks.worker), not a URL host.
SUBJECT_WORKER = "worker"
# An applicant-tracking system family (greenhouse, lever, ...), not one host.
SUBJECT_ATS = "ats"
# A task KIND (extract_comp), not a task id.
SUBJECT_TASK_KIND = "task_kind"
# One of the detector sections in this module, when it is the thing broken.
SUBJECT_DETECTOR = "detector"

_SUBJECT_KINDS = {
    "ats_text_collapse": SUBJECT_SOURCE,
    "extraction_failing": SUBJECT_HOST,
    "oauth_token_invalid": SUBJECT_PROVIDER_USER,
    "batch_parked_too_long": SUBJECT_TASK_KIND,
    "batch_failed_whole": SUBJECT_PURPOSE,
    "ingest_failing": SUBJECT_SOURCE,
    "ingest_host_failing": SUBJECT_HOST,
    "source_feed_empty": SUBJECT_SOURCE,
    "source_pattern_excludes_all": SUBJECT_SOURCE,
    "worker_fetches_failing": SUBJECT_WORKER,
    "resolver_bypassed": SUBJECT_ATS,
    "queue_stalled": SUBJECT_WORKER,
    "ingest_backlog": SUBJECT_TASK_KIND,
    "fleet_mixed_release": SUBJECT_WORKER,
    "source_pattern_admits_all": SUBJECT_SOURCE,
    "sweep_did_nothing": SUBJECT_TASK_KIND,
    "task_progress_invalid": SUBJECT_TASK_KIND,
    "task_progress_stalled": SUBJECT_TASK,
    "task_kind_failing": SUBJECT_TASK_KIND,
    "task_requeued_forever": SUBJECT_TASK,
    "alerts_unnotified": SUBJECT_DETECTOR,
    "address_blocked_by_host": SUBJECT_HOST,
    "detector_failed": SUBJECT_DETECTOR,
}


def subject_kind_for(alert_kind: str) -> str | None:
    """What this kind of alert puts in its subject, or None when unknown.

    None rather than a default: a wrong link is worse than no link, which is
    the whole reason this exists. A detector added without an entry here gets
    plain text, which is honest, instead of inheriting whatever the last one
    used.

    The rate-spike kinds are generated per check_type, so they are matched by
    shape rather than listed - listing them would mean a new check_type
    silently losing its link.
    """
    if alert_kind.endswith("_rate_spike"):
        return SUBJECT_SOURCE
    return _SUBJECT_KINDS.get(alert_kind)
