"""Whose addresses are whose, and the mail the owner sent.

The classifier booked 1,239 consumed events on mail the owner wrote, because
the self-sent guard tested one address while the mailbox owns two. Direction is
a header fact, so none of this needs a model - it needs knowing which addresses
are his.
"""

from __future__ import annotations

from api import db
from api.tasks.mail_classify import _SELF_SENT, _heal_self_sent, identities_for
from core.identity import MAX_IDENTITIES, AddressCount, derive_identities


class TestDerivingTheIdentitySet:
    def test_two_addresses_of_similar_size_stay_together(self):
        """The real corpus: 34,022 and 22,846 are one person, and the drop to
        the busiest correspondent at 5,322 is the largest gap in the list."""
        found = derive_identities(
            [
                AddressCount("kanishksachdev@gmail.com", 34022),
                AddressCount("kqs6171@psu.edu", 22846),
                AddressCount("notifications@github.com", 5322),
                AddressCount("notifications@instructure.com", 4059),
            ],
            67257,
        )
        assert found == {"kanishksachdev@gmail.com", "kqs6171@psu.edu"}

    def test_a_single_address_mailbox_returns_one(self):
        found = derive_identities(
            [AddressCount("solo@x.com", 900), AddressCount("a@y.com", 40)], 1000
        )
        assert found == {"solo@x.com"}

    def test_three_identities_are_all_kept(self):
        """The cut is adaptive, not a count. A rule that returned two would be
        a threshold wearing a gap's clothes."""
        found = derive_identities(
            [
                AddressCount("a@x", 900),
                AddressCount("b@x", 800),
                AddressCount("c@x", 700),
                AddressCount("corr@y", 50),
            ],
            1000,
        )
        assert found == {"a@x", "b@x", "c@x"}

    def test_the_tail_cannot_win_the_gap(self):
        """Two messages beside one is a 2.0x ratio and means nothing. The floor
        exists only to keep quantization out of the search - it is not what
        separates owners from correspondents."""
        counts = [AddressCount("owner@x", 800), AddressCount("corr@y", 300)]
        counts += [AddressCount(f"tail{i}@z", 2 if i % 2 else 1) for i in range(40)]
        assert derive_identities(counts, 1000) == {"owner@x"}

    def test_a_mailbox_too_small_to_judge_returns_nothing(self):
        """An empty set means "fall back to what you were told" rather than a
        guess. Three messages say nothing about who owns them."""
        assert derive_identities([AddressCount("a@x.com", 2)], 0) == set()

    def test_the_result_is_bounded(self):
        counts = [AddressCount(f"a{i}@x", 1000 - i) for i in range(20)]
        assert len(derive_identities(counts, 1000)) <= MAX_IDENTITIES

    def test_addresses_come_back_lowercased(self):
        found = derive_identities([AddressCount("MiXeD@X.com", 900)], 1000)
        assert found == {"mixed@x.com"}


class TestTheGuard:
    def _msg(self, f, **kw):
        uid = kw.pop("user_id", None) or f.make_user()
        row = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
            "to_emails, subject, body_text) VALUES (%s, %s, 'olm', %s, %s, %s, 'body') "
            "RETURNING id",
            (
                uid,
                kw.get(
                    "mid",
                    f"m{db.query_one('SELECT COALESCE(MAX(id),0)+1 AS n FROM email_messages')['n']}",
                ),
                kw.get("from_email"),
                kw.get("to_emails", []),
                kw.get("subject", "s"),
            ),
        )
        assert row is not None
        return row["id"], uid

    def _is_self_sent(self, mid: int, identities: list[str]) -> bool:
        row = db.query_one(
            f"SELECT {_SELF_SENT} AS s FROM email_messages m WHERE m.id = %(mid)s",
            {"mid": mid, "identities": identities},
        )
        assert row is not None
        return row["s"]

    def test_a_second_owner_address_is_caught(self, f):
        """The whole defect: 1,840 messages were sent from the address the
        guard did not know about."""
        mid, _ = self._msg(f, from_email="kqs6171@psu.edu")
        assert self._is_self_sent(mid, ["kanishksachdev@gmail.com", "kqs6171@psu.edu"])
        assert not self._is_self_sent(mid, ["kanishksachdev@gmail.com"])

    def test_an_inbound_message_is_not_caught(self, f):
        mid, _ = self._msg(f, from_email="recruiter@company.com")
        assert not self._is_self_sent(mid, ["kqs6171@psu.edu"])

    def test_a_sent_item_with_no_sender_is_caught(self, f):
        """252 OLM messages carry no from_email. One with no sender that is not
        addressed to you is one you sent - if it had arrived, you would be on
        it."""
        mid, _ = self._msg(f, from_email=None, to_emails=["someone@else.com"])
        assert self._is_self_sent(mid, ["kqs6171@psu.edu"])

    def test_a_message_with_no_sender_addressed_to_the_owner_is_not_caught(self, f):
        """The 11 that are addressed to him are inbound mail whose sender
        failed to parse, and guessing about them would be the model's job, not
        a rule's."""
        mid, _ = self._msg(f, from_email=None, to_emails=["KQS6171@psu.edu"])
        assert not self._is_self_sent(mid, ["kqs6171@psu.edu"])

    def test_the_match_is_case_insensitive(self, f):
        mid, _ = self._msg(f, from_email="Kanishk Sachdev <KQS6171@PSU.EDU>")
        assert self._is_self_sent(mid, ["kqs6171@psu.edu"])


class TestHealing:
    def _classified(self, f, from_email: str, kind: str):
        row = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
            "to_emails, subject, body_text) VALUES (%s, %s, 'olm', %s, '{}', 's', 'b') "
            "RETURNING id",
            (f.make_user(), f"h{kind}{from_email}", from_email),
        )
        assert row is not None
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
            "VALUES (%s, %s, 'high', '{}'::jsonb, 'gpt-5.6-luna')",
            (row["id"], kind),
        )
        return row["id"]

    def _latest(self, mid: int):
        return db.query_one(
            "SELECT kind, detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
            (mid,),
        )

    def test_an_event_on_the_owners_own_mail_is_superseded(self, f):
        """Widening the guard stops new errors; the existing ones stand until
        something retracts them."""
        mid = self._classified(f, "kqs6171@psu.edu", "interview_invite")
        assert _heal_self_sent(["kqs6171@psu.edu"]) == 1
        latest = self._latest(mid)
        assert latest is not None
        assert latest["kind"] == "not_job_related"
        assert latest["detail"]["reason"] == "self_sent"

    def test_nothing_is_deleted(self, f):
        """Append-only: what was retracted stays readable, which is what makes
        the admin view able to say what changed and why."""
        mid = self._classified(f, "kqs6171@psu.edu", "offer")
        _heal_self_sent(["kqs6171@psu.edu"])
        kinds = [
            r["kind"]
            for r in db.query(
                "SELECT kind FROM email_events WHERE message_id = %s ORDER BY id", (mid,)
            )
        ]
        assert kinds == ["offer", "not_job_related"]

    def test_inbound_mail_is_left_alone(self, f):
        mid = self._classified(f, "recruiter@company.com", "interview_invite")
        assert _heal_self_sent(["kqs6171@psu.edu"]) == 0
        latest = self._latest(mid)
        assert latest is not None and latest["kind"] == "interview_invite"

    def test_it_converges_instead_of_rewriting_every_sweep(self, f):
        """A correction that re-fires forever would grow the table without
        bound and make 'what changed' unreadable."""
        self._classified(f, "kqs6171@psu.edu", "interview_invite")
        assert _heal_self_sent(["kqs6171@psu.edu"]) == 1
        assert _heal_self_sent(["kqs6171@psu.edu"]) == 0

    def test_a_widened_identity_set_heals_its_own_history(self, f):
        """The reason this is a sweep and not a one-off backfill: the set is
        derived, so a new address should correct its own past without anyone
        running anything."""
        mid = self._classified(f, "kqs6171@psu.edu", "interview_scheduled")
        assert _heal_self_sent(["kanishksachdev@gmail.com"]) == 0
        assert _heal_self_sent(["kanishksachdev@gmail.com", "kqs6171@psu.edu"]) == 1
        latest = self._latest(mid)
        assert latest is not None and latest["kind"] == "not_job_related"


def test_identities_fall_back_to_the_configured_address(f):
    """A mailbox with too little mail to judge must not switch the guard off."""
    uid = f.make_user(email="only@known.com")
    assert identities_for(uid) == ["only@known.com"]
