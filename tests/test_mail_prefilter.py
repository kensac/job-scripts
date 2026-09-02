"""The pre-filter decides what 38,685 messages cost, and what is lost forever.

It is deliberately lopsided. A missed email is an outcome that cannot be
recovered - the posting is gone and the thread is not coming back. A false
positive costs one cheap classification the model then rejects. So these tests
are mostly about RECALL, and the precision cases exist only to stop it
degenerating into "everything".
"""

from __future__ import annotations

import pytest

from core.mail_prefilter import ATS_DOMAINS, looks_job_related


def _hit(**kw) -> bool:
    return looks_job_related(
        from_email=kw.get("from_email"),
        subject=kw.get("subject"),
        body=kw.get("body", ""),
    ).hit


@pytest.mark.parametrize(
    ("from_email", "subject", "body"),
    [
        # Sender alone is enough: ATS mail often has a neutral subject.
        ("no-reply@greenhouse.io", "Update", ""),
        ("no-reply@us.greenhouse-mail.io", "", ""),
        ("noreply@hire.lever.co", "", ""),
        ("notifications@ashbyhq.com", "", ""),
        ("do-not-reply@myworkday.com", "", ""),
        # A rejection from an employer's own domain, substance in the body.
        ("careers@stripe.com", "Update on your application", "we regret to inform you"),
        # An assessment invite that never says "application".
        ("recruiting@acme.com", "Next steps", "Please complete the HackerRank challenge"),
        # Scheduling, which is where interviews actually get confirmed.
        ("hr@acme.com", "Chat?", "Could you share your availability for a phone screen"),
        # Only evidence is a link back to a board.
        ("someone@unknown.test", "hi", "see https://boards.greenhouse.io/acme/jobs/123"),
    ],
)
def test_recall_on_application_mail(from_email, subject, body):
    assert _hit(from_email=from_email, subject=subject, body=body) is True


@pytest.mark.parametrize(
    ("from_email", "subject", "body"),
    [
        ("noreply@github.com", "[repo] PR merged", "the build passed"),
        ("team@vercel.com", "Deployment ready", "your deployment is live"),
        ("alerts@chase.com", "Statement ready", "your statement is available"),
        ("info@airline.com", "Your flight", "check in now"),
    ],
)
def test_ordinary_mail_is_not_swept_in(from_email, subject, body):
    """Not correctness so much as cost control: this is what keeps the
    classification bill proportional to the job mail rather than the mailbox."""
    assert _hit(from_email=from_email, subject=subject, body=body) is False


def test_reason_identifies_the_rule_that_fired():
    """Stored on the row so a later widening can tell what was kept and why,
    and so a human auditing false positives can see WHICH rule is too loose
    rather than re-deriving it from the whole set."""
    v = looks_job_related(from_email="no-reply@greenhouse.io", subject="", body="")
    assert v.reason.startswith("ats_domain:")
    v = looks_job_related(from_email="a@b.test", subject="Your application", body="")
    assert v.reason == "phrase:your application"
    v = looks_job_related(from_email="a@b.test", subject="", body="")
    assert v.reason == "no_signal"


def test_ats_domains_are_inherited_from_the_resolvers():
    """core.ats already knows which hosts are ATSes. Restating them here would
    be a second list free to drift from the one the matcher uses."""
    from core.ats import RESOLVERS

    for resolver in RESOLVERS:
        for marker in resolver.markers:
            if "=" in marker:  # query-param markers are not domains
                continue
            assert marker in ATS_DOMAINS, f"{marker} from {resolver.name} not inherited"


def test_missing_fields_do_not_raise():
    """Archive imports produce partial messages - no From, no body, an
    unparsable header. A crash here would abort a 38,685-message import."""
    assert looks_job_related(from_email=None, subject=None, body=None).hit is False
    assert looks_job_related(from_email="", subject="", body="").hit is False
    assert looks_job_related(from_email="malformed", subject=None, body=None).hit is False
